"""Versioned corpus loading and explicit, content-addressed source planning.

The resolver deliberately separates planning from execution. Tests can inspect
the exact argv without network access; public source is only fetched after a
caller supplies an explicit confirmation flag.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..evals.models import BenchmarkCase
from ..evidence.canonical import canonical_json, hash_text


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        shell: bool = False,
        timeout_seconds: int = 120,
    ) -> Any: ...


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class SubprocessCommandRunner:
    """Small injectable runner used only after a caller confirms egress."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        shell: bool = False,
        timeout_seconds: int = 120,
    ) -> CommandResult:
        if shell:
            raise ValueError("corpus commands must use shell=False")
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("corpus command argv must contain non-empty strings")
        try:
            result = subprocess.run(
                list(argv),
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(list(argv), 124, str(exc.stdout or ""), str(exc.stderr or ""))
        return CommandResult(list(argv), result.returncode, result.stdout, result.stderr)


@dataclass(frozen=True)
class FetchPlan:
    case_id: str
    cache_key: str
    cache_path: Path
    source_url: str | None
    revision: str | None
    commands: tuple[tuple[str, ...], ...] = ()
    network_required: bool = False
    confirmation_required: bool = False
    status: str = "ready"
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "cache_key": self.cache_key,
            "cache_path": str(self.cache_path),
            "source_url": self.source_url,
            "revision": self.revision,
            "commands": [list(command) for command in self.commands],
            "network_required": self.network_required,
            "confirmation_required": self.confirmation_required,
            "status": self.status,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def canonical_case_payload(case: BenchmarkCase) -> str:
    return canonical_json(case.model_dump(mode="json"))


def content_addressed_cache_key(case: BenchmarkCase) -> str:
    return hash_text(canonical_case_payload(case))


def load_cases(path: str | Path, *, allow_unresolved: bool = True) -> list[BenchmarkCase]:
    """Load a v2 corpus file and validate every case before planning."""

    manifest_path = Path(path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = raw.get("cases", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("corpus manifest must be a list or an object with cases")
    cases = [BenchmarkCase.model_validate(item) for item in items]
    if not allow_unresolved:
        unresolved = [case.id for case in cases if case.provenance_state == "unresolved"]
        if unresolved:
            raise ValueError(f"unresolved public cases require resolver preflight: {unresolved}")
    return cases


def build_fetch_plan(
    case: BenchmarkCase,
    cache_root: str | Path,
    *,
    confirm_download: bool = False,
) -> FetchPlan:
    """Build a safe argv-only plan; this function never downloads anything."""

    cache_root = Path(cache_root).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    key = content_addressed_cache_key(case)
    destination = cache_root / key
    if case.source_kind == "local":
        local = Path(case.local_path or "")
        if not local.is_absolute():
            local = (Path.cwd() / local).resolve()
        return FetchPlan(
            case.id,
            key,
            destination,
            None,
            None,
            status="local",
            reason="PatchProof-owned fixture; no public-code egress",
            evidence={"local_path": str(local)},
        )
    if case.provenance_state == "unresolved":
        return FetchPlan(
            case.id,
            key,
            destination,
            case.source_url or case.repo_url,
            None,
            status="unresolved",
            reason="official descriptor requires resolver verification before download",
            evidence={"resolver_requirements": case.resolver_requirements},
        )
    if not case.repo_url or not case.immutable_revision:
        raise ValueError(f"case {case.id} is missing a resolved repository URL/revision")
    source_key = hash_text(f"{case.repo_url}@{case.immutable_revision.lower()}")
    resolved_source = cache_root / "sources" / source_key
    if resolved_source.is_dir():
        return FetchPlan(
            case.id,
            source_key,
            resolved_source,
            case.repo_url,
            case.immutable_revision,
            status="cached",
            reason="resolver source cache exists; verify HEAD without network",
            evidence={"cache_hit": True, "cache_kind": "resolved_source"},
        )
    if destination.is_dir():
        return FetchPlan(
            case.id,
            key,
            destination,
            case.repo_url,
            case.immutable_revision,
            status="cached",
            reason="content-addressed checkout exists; verify HEAD without network",
            evidence={"cache_hit": True},
        )
    commands: tuple[tuple[str, ...], ...] = (
        ("git", "clone", "--no-checkout", case.repo_url, str(destination)),
        ("git", "-C", str(destination), "checkout", "--detach", case.immutable_revision),
    )
    if not confirm_download:
        return FetchPlan(
            case.id,
            key,
            destination,
            case.repo_url,
            case.immutable_revision,
            commands=commands,
            network_required=True,
            confirmation_required=True,
            status="confirmation_required",
            reason="public source download requires --confirm-download",
        )
    return FetchPlan(
        case.id,
        key,
        destination,
        case.repo_url,
        case.immutable_revision,
        commands=commands,
        network_required=True,
        confirmation_required=False,
        status="ready",
        reason="explicitly confirmed immutable checkout",
    )


def execute_fetch_plan(
    plan: FetchPlan,
    *,
    runner: CommandRunner | None = None,
    confirm_download: bool = False,
) -> dict[str, Any]:
    """Execute a previously planned checkout with argv and verify its HEAD."""

    if plan.status in {"local", "unresolved"}:
        return {"status": plan.status, "case_id": plan.case_id, "evidence": plan.evidence}
    if plan.confirmation_required and not confirm_download:
        return {
            "status": "confirmation_required",
            "case_id": plan.case_id,
            "reason": plan.reason,
            "commands": [list(command) for command in plan.commands],
        }
    command_runner = runner or SubprocessCommandRunner()
    results: list[dict[str, Any]] = []
    for command in plan.commands:
        raw = _run(command_runner, command, cwd=None)
        result = _as_result(command, raw)
        results.append(result)
        if result["returncode"] != 0:
            return {"status": "failed", "case_id": plan.case_id, "commands": results}
    head_command = ("git", "-C", str(plan.cache_path), "rev-parse", "HEAD")
    raw = _run(command_runner, head_command, cwd=None)
    head_result = _as_result(head_command, raw)
    results.append(head_result)
    actual = head_result["stdout"].strip()
    verified = head_result["returncode"] == 0 and actual.lower() == str(plan.revision).lower()
    return {
        "status": "ready" if verified else "revision_mismatch",
        "case_id": plan.case_id,
        "cache_path": str(plan.cache_path),
        "expected_revision": plan.revision,
        "actual_revision": actual or None,
        "revision_verified": verified,
        "commands": results,
    }


def _run(runner: CommandRunner, argv: Sequence[str], *, cwd: str | Path | None) -> Any:
    try:
        return runner.run(argv, cwd=cwd, shell=False)
    except TypeError:
        # Keep simple test doubles useful while preserving the production
        # runner's explicit shell=False contract.
        return runner.run(argv, cwd=cwd)


def _as_result(argv: Sequence[str], raw: Any) -> dict[str, Any]:
    return {
        "argv": list(argv),
        "returncode": int(getattr(raw, "returncode", raw.get("returncode", 1) if isinstance(raw, dict) else 1)),
        "stdout": str(getattr(raw, "stdout", raw.get("stdout", "") if isinstance(raw, dict) else "")),
        "stderr": str(getattr(raw, "stderr", raw.get("stderr", "") if isinstance(raw, dict) else "")),
    }


class CorpusResolver:
    def __init__(self, cache_root: str | Path, *, runner: CommandRunner | None = None):
        self.cache_root = Path(cache_root).resolve()
        self.runner = runner or SubprocessCommandRunner()

    def plan(self, case: BenchmarkCase, *, confirm_download: bool = False) -> FetchPlan:
        return build_fetch_plan(case, self.cache_root, confirm_download=confirm_download)

    def resolve(self, case: BenchmarkCase, *, confirm_download: bool = False) -> dict[str, Any]:
        plan = self.plan(case, confirm_download=confirm_download)
        return execute_fetch_plan(plan, runner=self.runner, confirm_download=confirm_download)


CacheManager = CorpusResolver
FetchPlanner = CorpusResolver
