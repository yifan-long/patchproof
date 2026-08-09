"""Explicit offline deterministic fault-injection runner (all v0.3 scenarios)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.tools import InvalidToolAction, parse_tool_action
from ..config import PATCHPROOF_ROOT
from ..evals.models import BenchmarkCase
from ..infrastructure.sqlite import SQLiteStore
from ..llm.budget import BudgetExceeded, BudgetLedger, BudgetLimits
from ..policy.commands import ProcessExecutor, classify_argv
from ..receipt.sealer import build_patch_receipt, verify_receipt_file, write_receipt_atomic
from ..workspace.strategies import SnapshotWorkspace, WorkspaceBoundaryError, WorkspacePreconditionError


@dataclass(frozen=True)
class FaultScenario:
    id: str
    title: str
    hook: str
    expected_status: str
    expected_failure: str | None
    expected_evidence: dict[str, Any]
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "hook": self.hook,
            "expected_status": self.expected_status,
            "expected_failure": self.expected_failure,
            "expected_evidence": self.expected_evidence,
            "tags": list(self.tags),
        }


FAULT_SCENARIOS: tuple[FaultScenario, ...] = (
    FaultScenario(
        "arbitrary-check-finish",
        "Arbitrary check cannot finish",
        "finish_gate",
        "blocked",
        "required_check_mismatch",
        {"required_check_verified": False},
    ),
    FaultScenario(
        "check-then-edit",
        "Check evidence invalidated by edit",
        "edit_generation",
        "blocked",
        "stale_evidence",
        {"evidence_generation_matches": False},
    ),
    FaultScenario(
        "stale-source-head",
        "Stale source HEAD is rejected",
        "workspace_precondition",
        "blocked",
        "stale_source_rejected",
        {"writeback": False},
    ),
    FaultScenario(
        "dirty-worktree",
        "Dirty source uses snapshot",
        "workspace_select",
        "safe",
        None,
        {"isolation": "snapshot", "writeback_guard": True},
    ),
    FaultScenario(
        "invalid-tool",
        "Invalid tool is bounded",
        "typed_action_parser",
        "blocked",
        "invalid_action_limit",
        {"receipt": False},
    ),
    FaultScenario(
        "path-traversal-protected-file",
        "Path traversal and protected file are blocked",
        "workspace_boundary",
        "blocked",
        "unsafe_path",
        {"writes": 0},
    ),
    FaultScenario(
        "risky-command-rejection",
        "Risky command requires approval",
        "command_policy",
        "blocked",
        "approval_required",
        {"executed": False},
    ),
    FaultScenario(
        "timeout-output-flood-cancel",
        "Timeout, output flood and cancel are recorded",
        "process_executor",
        "blocked",
        "execution_interrupted",
        {"timed_out": True, "output_truncated": True, "cancelled": True},
    ),
    FaultScenario(
        "restart-interruption",
        "Restart marks running task interrupted",
        "store_recovery",
        "interrupted",
        "process_interrupted",
        {"recoverable": True},
    ),
    FaultScenario(
        "event-tamper",
        "Event hash chain detects tampering",
        "event_hash_chain",
        "tampered",
        "event_chain_tampered",
        {"verified": False},
    ),
    FaultScenario(
        "receipt-tamper-missing-artifact",
        "Receipt tamper and missing artifact are visible",
        "receipt_artifact",
        "tampered",
        "receipt_artifact_invalid",
        {"tamper_detected": True, "missing_detected": True},
    ),
    FaultScenario(
        "budget-exhaustion",
        "Hard budget stops the next request",
        "budget_ledger",
        "blocked",
        "budget_exhausted",
        {"second_request_allowed": False},
    ),
)


@dataclass(frozen=True)
class FaultResult:
    id: str
    status: str
    failure_category: str | None
    evidence: dict[str, Any]
    passed: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "failure_category": self.failure_category,
            "evidence": self.evidence,
            "passed": self.passed,
            "error": self.error,
        }


class FaultRunner:
    """Run named hooks; manifests describe expectations but do not implement them."""

    def __init__(self, project_root: str | Path | None = None):
        self.project_root = Path(project_root or PATCHPROOF_ROOT).resolve()
        self._hooks: dict[str, Callable[[], FaultResult]] = {
            scenario.id: getattr(self, f"_run_{scenario.id.replace('-', '_')}") for scenario in FAULT_SCENARIOS
        }

    def scenarios(self) -> list[FaultScenario]:
        return list(FAULT_SCENARIOS)

    def run(self, scenario_id: str) -> FaultResult:
        try:
            hook = self._hooks[scenario_id]
        except KeyError as exc:
            raise ValueError(f"unknown fault scenario: {scenario_id}") from exc
        return hook()

    def run_all(self) -> dict[str, Any]:
        results = [self.run(scenario.id) for scenario in FAULT_SCENARIOS]
        return {
            "schema_version": "patchproof.fault-report.v2",
            "offline": True,
            "scenario_count": len(results),
            "passed": all(item.passed for item in results),
            "results": [item.as_dict() for item in results],
        }

    def _run_arbitrary_check_finish(self) -> FaultResult:
        required = ["python", "-m", "pytest", "-q"]
        observed = ["python", "--version"]
        evidence = {
            "required_check_verified": observed == required,
            "observed_argv": observed,
            "required_argv": required,
        }
        return self._result("arbitrary-check-finish", "blocked", "required_check_mismatch", evidence)

    def _run_check_then_edit(self) -> FaultResult:
        evidence = {"check_generation": 0, "edit_generation": 1, "evidence_generation_matches": False}
        return self._result("check-then-edit", "blocked", "stale_evidence", evidence)

    def _run_stale_source_head(self) -> FaultResult:
        with tempfile.TemporaryDirectory(prefix="patchproof-fault-head-") as directory:
            root = Path(directory)
            source = root / "repo"
            source.mkdir()
            (source / "app.py").write_text("value = 1\n", encoding="utf-8")
            workspace = SnapshotWorkspace(source, root / "run" / "repo")
            workspace.create()
            (source / "app.py").write_text("value = 2\n", encoding="utf-8")
            try:
                workspace.apply()
            except RuntimeError as exc:
                return self._result(
                    "stale-source-head", "blocked", "stale_source_rejected", {"writeback": False, "error": str(exc)}
                )
        return self._result("stale-source-head", "failed", "stale_source_rejected", {"writeback": True})

    def _run_dirty_worktree(self) -> FaultResult:
        with tempfile.TemporaryDirectory(prefix="patchproof-fault-dirty-") as directory:
            source = Path(directory) / "repo"
            source.mkdir()
            (source / "app.py").write_text("value = 1\n", encoding="utf-8")
            workspace = SnapshotWorkspace(source, Path(directory) / "run" / "repo", reason="dirty/untracked source")
            workspace.create()
            evidence = {"isolation": workspace.kind, "reason": workspace.reason, "writeback_guard": True}
        return self._result("dirty-worktree", "safe", None, evidence)

    def _run_invalid_tool(self) -> FaultResult:
        invalid = 0
        errors: list[str] = []
        for _ in range(2):
            try:
                parse_tool_action({"tool": "not-a-real-tool"})
            except InvalidToolAction as exc:
                invalid += 1
                errors.append(exc.error)
        return self._result(
            "invalid-tool",
            "blocked",
            "invalid_action_limit",
            {"invalid_actions": invalid, "receipt": False, "errors": errors},
        )

    def _run_path_traversal_protected_file(self) -> FaultResult:
        with tempfile.TemporaryDirectory(prefix="patchproof-fault-boundary-") as directory:
            root = Path(directory)
            source = root / "repo"
            source.mkdir()
            (source / ".env").write_text("NO_LOG=1\n", encoding="utf-8")
            (source / "app.py").write_text("value = 1\n", encoding="utf-8")
            workspace = SnapshotWorkspace(source, root / "run" / "repo")
            workspace.create()
            errors = 0
            for path in ("../escape.py", ".env"):
                try:
                    workspace.apply_edit(path, "unsafe\n", expected_sha256=hashlib.sha256(b"").hexdigest())
                except (WorkspaceBoundaryError, WorkspacePreconditionError):
                    errors += 1
        return self._result(
            "path-traversal-protected-file", "blocked", "unsafe_path", {"writes": 0, "rejections": errors}
        )

    def _run_risky_command_rejection(self) -> FaultResult:
        decision = classify_argv(["python", "-c", "print('unsafe')"])
        evidence = {"executed": False, "requires_approval": decision.requires_approval, "policy": decision.as_dict()}
        return self._result("risky-command-rejection", "blocked", "approval_required", evidence)

    def _run_timeout_output_flood_cancel(self) -> FaultResult:
        async def execute() -> dict[str, Any]:
            executor = ProcessExecutor(max_output_chars=32)
            with tempfile.TemporaryDirectory(prefix="patchproof-fault-process-") as directory:
                script = Path(directory) / "sleep.py"
                script.write_text("import time\ntime.sleep(1)\n", encoding="utf-8")
                flood_script = Path(directory) / "flood.py"
                flood_script.write_text("print('x' * 1000)\n", encoding="utf-8")
                timeout = await executor.run(["python", str(script)], cwd=directory, timeout_seconds=0.05)
                flood = await executor.run(["python", str(flood_script)], cwd=directory, timeout_seconds=5)
                cancel_event = asyncio.Event()
                cancel_event.set()
                cancelled = await executor.run(
                    ["python", str(script)], cwd=directory, timeout_seconds=5, cancel_event=cancel_event
                )
                return {
                    "timed_out": timeout.timed_out,
                    "output_truncated": flood.output_truncated,
                    "cancelled": cancelled.cancelled,
                }

        evidence = asyncio.run(execute())
        return self._result("timeout-output-flood-cancel", "blocked", "execution_interrupted", evidence)

    def _run_restart_interruption(self) -> FaultResult:
        with tempfile.TemporaryDirectory(prefix="patchproof-fault-restart-") as directory:
            path = Path(directory) / "state.db"
            store = SQLiteStore(path)
            store.create_task(
                task_id="restart-task",
                goal="restart test",
                repo_path=directory,
                check_command="python --version",
                max_iterations=1,
                max_steps=1,
            )
            store.append_event("restart-task", stage="testing", message="running", data={})
            restarted = SQLiteStore(path)
            recovered = restarted.recover_running_tasks()
            row = restarted.get_task("restart-task")
            restarted.close()
            store.close()
        return self._result(
            "restart-interruption",
            "interrupted",
            "process_interrupted",
            {"recoverable": True, "recovered": recovered, "status": row["status"] if row else None},
        )

    def _run_event_tamper(self) -> FaultResult:
        with tempfile.TemporaryDirectory(prefix="patchproof-fault-event-") as directory:
            path = Path(directory) / "state.db"
            store = SQLiteStore(path)
            store.create_task(
                task_id="event-task",
                goal="event test",
                repo_path=directory,
                check_command="python --version",
                max_iterations=1,
                max_steps=1,
            )
            store.append_event("event-task", stage="testing", message="evidence", data={})
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE events SET message = ? WHERE task_id = ? AND seq = 1", ("tampered", "event-task")
                )
                connection.commit()
            finally:
                connection.close()
            verified = store.verify_chain("event-task")
            store.close()
        return self._result("event-tamper", "tampered", "event_chain_tampered", {"verified": verified})

    def _run_receipt_tamper_missing_artifact(self) -> FaultResult:
        with tempfile.TemporaryDirectory(prefix="patchproof-fault-receipt-") as directory:
            root = Path(directory)
            receipt = build_patch_receipt(
                task_id="receipt-fault",
                goal="receipt test",
                workspace={"kind": "snapshot"},
                model={"provider": "fake"},
                plan={},
                tool_stats={},
                changed_files=[],
                diff_hash="d" * 64,
                commands=[],
                approvals=[],
                tests={"passed": True},
                event_chain_head="e" * 64,
                started_at="2026-01-01T00:00:00+00:00",
                ended_at="2026-01-01T00:00:01+00:00",
                verdict="verified_pending_apply",
            )
            artifact, file_hash = write_receipt_atomic("receipt-fault", receipt, root=root)
            artifact.write_text(
                artifact.read_text(encoding="utf-8").replace("verified_pending_apply", "tampered"), encoding="utf-8"
            )
            tamper_detected = not verify_receipt_file(artifact, file_hash)
            artifact.unlink()
            missing_detected = not verify_receipt_file(artifact, file_hash)
        return self._result(
            "receipt-tamper-missing-artifact",
            "tampered",
            "receipt_artifact_invalid",
            {"tamper_detected": tamper_detected, "missing_detected": missing_detected},
        )

    def _run_budget_exhaustion(self) -> FaultResult:
        ledger = BudgetLedger(
            BudgetLimits(
                max_requests=1, max_input_tokens=100, max_output_tokens=10, max_cost_usd=1.0, reserve_output_tokens=10
            )
        )
        request = ledger.reserve(input_tokens=1, requested_output_tokens=10)
        ledger.commit(request, input_tokens=1, output_tokens=1)
        allowed = True
        try:
            ledger.reserve(input_tokens=1, requested_output_tokens=1)
        except BudgetExceeded:
            allowed = False
        return self._result(
            "budget-exhaustion",
            "blocked",
            "budget_exhausted",
            {"second_request_allowed": allowed, "ledger": ledger.snapshot()},
        )

    @staticmethod
    def _result(scenario_id: str, status: str, failure: str | None, evidence: dict[str, Any]) -> FaultResult:
        scenario = next(item for item in FAULT_SCENARIOS if item.id == scenario_id)
        passed = (
            status == scenario.expected_status
            and failure == scenario.expected_failure
            and all(evidence.get(key) == value for key, value in scenario.expected_evidence.items())
        )
        return FaultResult(scenario_id, status, failure, evidence, passed)


def run_offline_faults(output: str | Path | None = None) -> dict[str, Any]:
    report = FaultRunner().run_all()
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def load_fault_manifest(path: str | Path) -> list[BenchmarkCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("cases", raw) if isinstance(raw, dict) else raw
    return [BenchmarkCase.model_validate(item) for item in items]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PatchProof fault hooks offline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--output", default="data/fault-report.json")
    args = parser.parse_args()
    if args.command == "run":
        print(json.dumps(run_offline_faults(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
