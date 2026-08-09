"""Injectable Docker evaluation execution with honest preflight evidence."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class DockerCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        shell: bool = False,
        timeout_seconds: int = 120,
    ) -> Any: ...


@dataclass(frozen=True)
class DockerLimits:
    cpu: float = 1.0
    memory: str = "1g"
    pids: int = 128
    timeout_seconds: int = 120
    output_chars: int = 12_000


@dataclass(frozen=True)
class DockerPreflight:
    cli_available: bool
    daemon_available: bool
    version: str | None
    image: str
    image_pinned: bool
    image_available: bool | None
    cache: dict[str, Any]
    registry: dict[str, Any]
    mirror: dict[str, Any]
    execution_mode: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cli_available": self.cli_available,
            "daemon_available": self.daemon_available,
            "version": self.version,
            "image": self.image,
            "image_pinned": self.image_pinned,
            "image_available": self.image_available,
            "cache": self.cache,
            "registry": self.registry,
            "mirror": self.mirror,
            "execution_mode": self.execution_mode,
            "reasons": self.reasons,
        }


@dataclass(frozen=True)
class DockerExecutionResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    isolation: str = "docker"
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "output_truncated": self.output_truncated,
            "isolation": self.isolation,
            "evidence": self.evidence,
        }


class DockerUnavailableError(RuntimeError):
    pass


class DockerEvalExecutor:
    """Build and execute deterministic, non-privileged Docker argv.

    The runner is injectable so unit tests can assert exact flags without a
    daemon. A local process runner is intentionally not a fallback here; the
    caller must choose a separate local-smoke executor and label its evidence.
    """

    def __init__(
        self,
        *,
        image: str,
        runner: DockerCommandRunner | None = None,
        docker_cli: str = "docker",
        limits: DockerLimits | None = None,
        registry: str | None = None,
        mirror: str | None = None,
        cache_root: str | Path | None = None,
        setup_network: str = "bridge",
    ):
        self.image = image
        self.runner = runner or SubprocessDockerRunner()
        self.docker_cli = docker_cli
        self.limits = limits or DockerLimits()
        self.registry = registry
        self.mirror = mirror
        self.cache_root = Path(cache_root).resolve() if cache_root else None
        self.setup_network = setup_network
        self._validate_image(image, allow_local=True)

    @staticmethod
    def _validate_image(image: str, *, allow_local: bool = False) -> None:
        if not image or not isinstance(image, str):
            raise ValueError("Docker image is required")
        if allow_local and image.startswith("local://"):
            return
        if ":latest" in image.lower() or image.endswith(":latest"):
            raise ValueError("Docker image must not use floating :latest")
        if not _is_pinned_image(image):
            raise ValueError("Docker image must be pinned by a sha256 digest")

    @staticmethod
    def _validate_workspace(workspace: str | Path) -> Path:
        path = Path(workspace).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"workspace mount must be an existing directory: {path}")
        return path

    @staticmethod
    def _validate_guest_argv(argv: Sequence[str]) -> list[str]:
        values = list(argv)
        if not values or any(not isinstance(item, str) or not item or "\x00" in item for item in values):
            raise ValueError("container command must be a non-empty argv list")
        if any("\r" in item or "\n" in item for item in values):
            raise ValueError("container command must not contain newlines")
        executable = Path(values[0]).name.lower()
        if executable in {"sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"}:
            raise ValueError("container command must not invoke a shell")
        if any(token in item for item in values for token in ("&&", "||", ";", "|", ">", "<")):
            raise ValueError("container argv must not contain shell composition")
        return values

    def build_run_argv(
        self,
        argv: Sequence[str],
        *,
        workspace: str | Path,
        image: str | None = None,
    ) -> list[str]:
        guest_argv = self._validate_guest_argv(argv)
        source = self._validate_workspace(workspace)
        selected_image = image or self.image
        self._validate_image(selected_image, allow_local=False)
        if "docker.sock" in str(source).lower():
            raise ValueError("Docker socket mounts are forbidden")
        return [
            self.docker_cli,
            "run",
            "--rm",
            "--pull",
            "never",
            "--init",
            "--read-only",
            "--network",
            "none",
            "--cpus",
            str(self.limits.cpu),
            "--memory",
            self.limits.memory,
            "--pids-limit",
            str(self.limits.pids),
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--env",
            "TZ=UTC",
            "--env",
            "LC_ALL=C.UTF-8",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PYTHONHASHSEED=0",
            "--mount",
            f"type=bind,src={source},dst=/workspace",
            "--workdir",
            "/workspace",
            selected_image,
            *guest_argv,
        ]

    def build_setup_argv(
        self,
        setup_argv: Sequence[str],
        *,
        workspace: str | Path,
        image: str | None = None,
        network: str | None = None,
    ) -> list[str]:
        """Build setup/build command separately; execution always uses none."""

        command = self.build_run_argv(setup_argv, workspace=workspace, image=image)
        network_name = network or self.setup_network
        if network_name not in {"none", "bridge", "host"}:
            raise ValueError("setup network must be an explicit Docker network mode")
        command[command.index("none")] = network_name
        return command

    def preflight(self) -> DockerPreflight:
        image_pinned = self.image.startswith("local://") or _is_pinned_image(self.image)
        version_result = self._run(
            (self.docker_cli, "version", "--format", "{{.Server.Version}}"),
            timeout_seconds=min(10, self.limits.timeout_seconds),
        )
        cli_available = version_result["returncode"] != 127
        daemon_available = version_result["returncode"] == 0
        version = version_result["stdout"].strip() or None
        if self.image.startswith("local://"):
            return DockerPreflight(
                cli_available=bool(cli_available),
                daemon_available=daemon_available,
                version=version,
                image=self.image,
                image_pinned=False,
                image_available=None,
                cache=_cache_state(self.cache_root),
                registry={"configured": bool(self.registry), "host": _host_only(self.registry)},
                mirror={"configured": bool(self.mirror), "host": _host_only(self.mirror)},
                execution_mode="local_smoke_only",
                reasons=["local:// image is a fixture marker; no Docker isolation claim is made"],
            )
        image_result: dict[str, Any] | None = None
        if daemon_available:
            image_result = self._run(
                (self.docker_cli, "image", "inspect", self.image),
                timeout_seconds=min(10, self.limits.timeout_seconds),
            )
        return DockerPreflight(
            cli_available=bool(cli_available),
            daemon_available=daemon_available,
            version=version,
            image=self.image,
            image_pinned=image_pinned,
            image_available=(image_result["returncode"] == 0 if image_result is not None else None),
            cache=_cache_state(self.cache_root),
            registry={"configured": bool(self.registry), "host": _host_only(self.registry)},
            mirror={"configured": bool(self.mirror), "host": _host_only(self.mirror)},
            execution_mode="docker_isolated" if daemon_available and image_pinned else "unavailable",
            reasons=[] if daemon_available else ["Docker daemon unavailable; public/real evaluation is blocked"],
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | Path,
        image: str | None = None,
        timeout_seconds: int | None = None,
        cancel_event: Any | None = None,
        output_limit: int | None = None,
    ) -> DockerExecutionResult:
        command = self.build_run_argv(argv, workspace=workspace, image=image)
        if cancel_event is not None and cancel_event.is_set():
            return DockerExecutionResult(command, 130, "", "cancelled before Docker start", 0, cancelled=True)
        preflight = self.preflight()
        if not preflight.daemon_available or not preflight.image_available:
            raise DockerUnavailableError("Docker daemon or pinned image is unavailable; local fallback is disabled")
        started = time.perf_counter()
        result = self._run(
            command,
            timeout_seconds=timeout_seconds or self.limits.timeout_seconds,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        limit = output_limit or self.limits.output_chars
        stdout, stdout_truncated = _trim(result["stdout"], limit)
        stderr, stderr_truncated = _trim(result["stderr"], limit)
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        timed_out = result["returncode"] == 124
        return DockerExecutionResult(
            argv=command,
            returncode=130 if cancelled else result["returncode"],
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=stdout_truncated or stderr_truncated,
            evidence={"preflight": preflight.as_dict(), "network": "none", "rootfs": "read-only"},
        )

    def run_setup(
        self,
        argv: Sequence[str],
        *,
        workspace: str | Path,
        timeout_seconds: int | None = None,
        network: str | None = None,
        output_limit: int | None = None,
    ) -> DockerExecutionResult:
        """Run an explicitly networked setup step and retain separate evidence."""

        command = self.build_setup_argv(argv, workspace=workspace, network=network)
        preflight = self.preflight()
        if not preflight.daemon_available or not preflight.image_available:
            raise DockerUnavailableError("Docker daemon or pinned image is unavailable for setup")
        started = time.perf_counter()
        result = self._run(command, timeout_seconds=timeout_seconds or self.limits.timeout_seconds)
        limit = output_limit or self.limits.output_chars
        stdout, stdout_truncated = _trim(result["stdout"], limit)
        stderr, stderr_truncated = _trim(result["stderr"], limit)
        return DockerExecutionResult(
            argv=command,
            returncode=result["returncode"],
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.perf_counter() - started) * 1000),
            output_truncated=stdout_truncated or stderr_truncated,
            evidence={
                "phase": "setup",
                "preflight": preflight.as_dict(),
                "network": command[command.index("--network") + 1],
                "execution_network": "none",
                "mirror_host": _host_only(self.mirror),
            },
        )

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        try:
            raw = self.runner.run(argv, shell=False, timeout_seconds=timeout_seconds)
        except TypeError:
            raw = self.runner.run(argv, shell=False)
        return {
            "argv": list(argv),
            "returncode": int(getattr(raw, "returncode", raw.get("returncode", 1) if isinstance(raw, dict) else 1)),
            "stdout": str(getattr(raw, "stdout", raw.get("stdout", "") if isinstance(raw, dict) else "")),
            "stderr": str(getattr(raw, "stderr", raw.get("stderr", "") if isinstance(raw, dict) else "")),
        }


class SubprocessDockerRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        shell: bool = False,
        timeout_seconds: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        if shell:
            raise ValueError("Docker commands require shell=False")
        try:
            return subprocess.run(
                list(argv),
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                return subprocess.CompletedProcess(list(argv), 124, str(exc.stdout or ""), str(exc.stderr or ""))
            return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


DockerExecutor = DockerEvalExecutor
DockerExecutionLayer = DockerEvalExecutor


class DockerProcessAdapter:
    """Async policy-executor shape backed by DockerEvalExecutor."""

    def __init__(self, executor: DockerEvalExecutor):
        self.executor = executor

    async def run(self, spec: Any, *, cwd: str, timeout_seconds: int = 120, cancel_event: Any = None) -> Any:
        from .policy import ExecutionResult

        argv = list(getattr(spec, "argv", spec))
        result = self.executor.run(
            argv,
            workspace=cwd,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        return ExecutionResult(
            argv=argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            output_truncated=result.output_truncated,
        )


def _is_pinned_image(image: str) -> bool:
    if image.startswith("sha256:"):
        digest = image.removeprefix("sha256:")
    elif "@sha256:" in image:
        digest = image.rsplit("@sha256:", 1)[-1]
    else:
        return False
    return len(digest) == 64 and all(char in "0123456789abcdefABCDEF" for char in digest)


def _trim(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[-limit:], True


def _host_only(value: str | None) -> str | None:
    if not value:
        return None
    return value.split("/", 1)[0].split(":", 1)[0]


def _cache_state(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {"configured": False, "exists": False, "entries": None}
    try:
        entries = sum(1 for child in root.iterdir() if child.is_dir()) if root.is_dir() else 0
    except OSError:
        entries = None
    return {"configured": True, "path": str(root), "exists": root.is_dir(), "entries": entries}
