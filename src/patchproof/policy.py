"""Command policy and shell-free process execution.

The policy is deliberately conservative. An argv that is not explicitly
classified as a read-only check is paused for human approval. This is a
policy gate, not a container: the process executor still runs on the local
machine and the threat-model documentation says so explicitly.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SHELL_META = re.compile(r"[;&|><`\^\r\n]")
HIGH_RISK = re.compile(
    r"(?:remove-item|del(?:\.exe)?|erase|rm(?:\.exe)?|rmdir|format(?:\.com|\.exe)?|"
    r"git\s+(?:reset|clean|checkout|push|commit|merge|rebase|restore)|"
    r"(?:pip|npm|pnpm|yarn|uv)\s+(?:install|add|remove|sync)|"
    r"(?:curl|wget|invoke-webrequest|start-process|powershell|pwsh|cmd(?:\.exe)?)|"
    r"(?:shutdown|reboot))")
NETWORK_RISK = re.compile(r"(?:curl|wget|invoke-webrequest|git\s+(?:clone|fetch|pull)|ssh|scp)")


class CommandSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    argv: list[str] = Field(min_length=1, max_length=32)

    @property
    def executable(self) -> str:
        return _basename(self.argv[0]).lower()

    @property
    def args(self) -> list[str]:
        return self.argv[1:]


@dataclass(frozen=True)
class CommandDecision:
    allowed: bool
    requires_approval: bool
    risk_level: str
    reason: str
    executable: str
    args: tuple[str, ...]
    argv: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "executable": self.executable,
            "args": list(self.args),
            "argv": list(self.argv),
        }


@dataclass(frozen=True)
class ExecutionResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "command": " ".join(shlex.quote(part) for part in self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "output_truncated": self.output_truncated,
        }


def _basename(value: str) -> str:
    return re.split(r"[\\/]", value)[-1]


def parse_command(command: str | list[str] | tuple[str, ...]) -> CommandSpec:
    if isinstance(command, str):
        if SHELL_META.search(command):
            raise ValueError("命令包含 shell 组合符，必须通过人工审批；typed tool 只接受单一 argv")
        try:
            argv = shlex.split(command, posix=False)
        except ValueError as exc:
            raise ValueError(f"命令解析失败: {exc}") from exc
    else:
        argv = list(command)
    if not argv or any(not isinstance(item, str) or not item.strip() for item in argv):
        raise ValueError("命令 argv 不能为空，且每个参数都必须是非空字符串")
    if any(SHELL_META.search(item) for item in argv):
        raise ValueError("argv 参数包含 shell 组合符")
    return CommandSpec(argv=[item.strip() for item in argv])


def normalize_command(command: str | list[str] | tuple[str, ...]) -> list[str]:
    """Parse a command once and return its canonical argv representation."""

    return list(parse_command(command).argv)


def _safe_readonly_check(spec: CommandSpec) -> bool:
    executable = spec.executable
    args = spec.args
    if executable in {"pytest", "pytest.exe"}:
        return True
    if executable in {"ruff", "ruff.exe"}:
        return bool(args) and args[0].lower() == "check"
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        if args in (["--version"], ["-V"]):
            return True
        if len(args) < 2 or args[0] != "-m":
            return False
        module = args[1].lower()
        return module in {"pytest", "unittest", "compileall"} or (
            module == "ruff" and len(args) >= 3 and args[2].lower() == "check"
        )
    if executable == "git.exe":
        executable = "git"
    return executable == "git" and bool(args) and args[0].lower() in {"diff", "status", "show", "log"}


def classify_argv(argv: list[str] | tuple[str, ...]) -> CommandDecision:
    try:
        spec = parse_command(argv)
    except ValueError as exc:
        raw = tuple(argv)
        executable = _basename(raw[0]) if raw else ""
        args = raw[1:] if raw else ()
        return CommandDecision(
            False,
            True,
            "high",
            str(exc),
            executable.lower(),
            args,
            raw,
        )
    normalized = " ".join(spec.argv).lower()
    if SHELL_META.search(normalized):
        return CommandDecision(
            False,
            True,
            "high",
            "命令包含 shell 组合符，可能拼接任意副作用",
            spec.executable,
            tuple(spec.args),
            tuple(spec.argv),
        )
    if _safe_readonly_check(spec):
        return CommandDecision(
            True,
            False,
            "low",
            "命令在只读检查白名单中",
            spec.executable,
            tuple(spec.args),
            tuple(spec.argv),
        )
    if HIGH_RISK.search(normalized):
        reason = "命令可能删除、安装、联网或修改 Git 工作树，需要人工审批"
        risk = "high"
    elif NETWORK_RISK.search(normalized):
        reason = "命令可能访问网络或外部环境，需要人工审批"
        risk = "high"
    else:
        reason = "命令不在只读检查白名单中，需要人工审批"
        risk = "medium"
    return CommandDecision(False, True, risk, reason, spec.executable, tuple(spec.args), tuple(spec.argv))


def classify_command(command: str) -> CommandDecision:
    try:
        return classify_argv(parse_command(command).argv)
    except ValueError as exc:
        raw = tuple(command.strip().split())
        return CommandDecision(False, True, "high", str(exc), _basename(raw[0]) if raw else "", raw[1:], raw)


class ProcessExecutor:
    """Local argv executor; this intentionally is not a Docker-grade sandbox."""

    def __init__(self, max_output_chars: int = 12_000):
        self.max_output_chars = max_output_chars

    async def run(
        self,
        spec: CommandSpec | list[str] | tuple[str, ...],
        *,
        cwd: str,
        timeout_seconds: int = 120,
        cancel_event: asyncio.Event | None = None,
    ) -> ExecutionResult:
        parsed = spec if isinstance(spec, CommandSpec) else parse_command(spec)
        started = asyncio.get_running_loop().time()
        execution_argv = _resolve_python_for_current_environment(parsed.argv)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *execution_argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
        )
        timed_out = False
        cancelled = False
        try:
            communicate = process.communicate()
            if cancel_event is None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(communicate, timeout=timeout_seconds)
            else:
                wait_task = asyncio.create_task(communicate)
                cancel_task = asyncio.create_task(cancel_event.wait())
                done, _pending = await asyncio.wait(
                    {wait_task, cancel_task},
                    timeout=timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    cancel_task.cancel()
                    await self._terminate(process)
                    stdout_bytes, stderr_bytes = await wait_task
                elif cancel_task in done and cancel_task.result():
                    cancelled = True
                    # Keep communicate alive so the terminated process's
                    # pipes are drained and the child cannot leak.
                    await self._terminate(process)
                    stdout_bytes, stderr_bytes = await wait_task
                else:
                    cancel_task.cancel()
                    stdout_bytes, stderr_bytes = await wait_task
        except TimeoutError:
            timed_out = True
            await self._terminate(process)
            stdout_bytes, stderr_bytes = await process.communicate()
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        stdout, stdout_truncated = _decode_and_trim(stdout_bytes or b"", self.max_output_chars)
        stderr, stderr_truncated = _decode_and_trim(stderr_bytes or b"", self.max_output_chars)
        return ExecutionResult(
            # Keep the policy-visible argv stable even when the local Python
            # interpreter is resolved to the current virtual environment.
            argv=list(parsed.argv),
            returncode=124 if timed_out else (130 if cancelled else process.returncode),
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=stdout_truncated or stderr_truncated,
        )

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, shell=False,
                )
                await killer.wait()
                return
            except OSError:
                pass
        try:
            process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=2)
        except (TimeoutError, ProcessLookupError):
            process.kill()


def _resolve_python_for_current_environment(argv: list[str]) -> list[str]:
    """Make ``python -m pytest`` use the interpreter running PatchProof.

    The command remains the user-visible/policy-visible argv. This only makes
    benchmark and local checks deterministic when ``python`` on PATH points to
    a different installation than the one containing the project dependencies.
    """

    if len(argv) >= 2 and _basename(argv[0]).lower() in {"python", "python.exe", "python3", "python3.exe"}:
        if argv[1] == "-m" and len(argv) >= 3 and argv[2].lower() in {"pytest", "compileall"}:
            return [sys.executable, *argv[1:]]
    return argv


def _decode_and_trim(value: bytes, limit: int) -> tuple[str, bool]:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[-limit:], True
