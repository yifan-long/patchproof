from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PATCHPROOF_ROOT, Settings
from .models import (
    TERMINAL_STATUSES,
    ApprovalSnapshot,
    ReceiptSnapshot,
    TaskEvent,
    TaskSnapshot,
    TaskStatus,
)
from .policy import CommandDecision, normalize_command
from .receipt import seal_receipt, verify_receipt, write_receipt_atomic
from .runner import AgentRunner
from .storage import SQLiteStore, json_dumps, json_loads, parse_datetime
from .workspace import open_workspace


def _now() -> datetime:
    return datetime.now(UTC)


ApprovalRequester = Callable[["TaskRecord", CommandDecision], Awaitable[ApprovalSnapshot]]


@dataclass
class TaskRecord:
    id: str
    goal: str
    repo_path: str
    check_command: str
    max_iterations: int
    max_steps: int
    status: TaskStatus = TaskStatus.QUEUED
    current_stage: str = TaskStatus.QUEUED.value
    iteration: int = 0
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    error: str | None = None
    failure_category: str | None = None
    plan: dict[str, Any] | None = None
    test_result: dict[str, Any] | None = None
    diff: str = ""
    changed_files: list[str] = field(default_factory=list)
    pending_command: list[str] | None = None
    pending_approval_id: str | None = None
    pending_risk: str | None = None
    pending_reason: str | None = None
    events: list[TaskEvent] = field(default_factory=list)
    approvals: list[ApprovalSnapshot] = field(default_factory=list)
    receipt: ReceiptSnapshot | None = None
    workspace_kind: str | None = None
    workspace_reason: str | None = None
    workspace_baseline: dict[str, Any] = field(default_factory=dict)
    tool_calls: int = 0
    invalid_actions: int = 0
    budget_used: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    required_check_argv: list[str] = field(default_factory=list)
    required_check_verified: bool = False
    required_check_evidence_generation: int | None = None
    edit_generation: int = 0
    required_check_last_result: dict[str, Any] | None = None
    precondition_failures: int = 0
    commands: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    approval_waiters: dict[str, asyncio.Event] = field(default_factory=dict)
    approval_results: dict[str, bool] = field(default_factory=dict)
    approval_requester: ApprovalRequester | None = None

    async def request_approval(self, decision: CommandDecision) -> ApprovalSnapshot:
        if self.approval_requester is None:
            raise RuntimeError("任务没有配置审批持久化回调")
        return await self.approval_requester(self, decision)

    async def wait_for_approval(self, approval_id: str) -> bool:
        waiter = self.approval_waiters.setdefault(approval_id, asyncio.Event())
        await waiter.wait()
        return self.approval_results.get(approval_id, False)

    @property
    def required_check_evidence_valid(self) -> bool:
        return (
            self.required_check_verified
            and self.required_check_evidence_generation is not None
            and self.required_check_evidence_generation == self.edit_generation
        )

    def snapshot(self, *, chain_head: str | None = None) -> TaskSnapshot:
        if self.receipt and self.receipt.artifact_path:
            from .receipt import verify_receipt_file

            self.receipt.file_verified = verify_receipt_file(
                self.receipt.artifact_path,
                self.receipt.file_sha256,
            )
            self.receipt.verified = verify_receipt(self.receipt.receipt, self.receipt.receipt_hash) and (
                self.receipt.file_verified
            )
        return TaskSnapshot(
            id=self.id,
            goal=self.goal,
            repo_path=self.repo_path,
            status=self.status,
            current_stage=self.current_stage,
            iteration=self.iteration,
            max_iterations=self.max_iterations,
            max_steps=self.max_steps,
            check_command=self.check_command,
            created_at=self.created_at,
            updated_at=self.updated_at,
            error=self.error,
            failure_category=self.failure_category,
            plan=self.plan,
            test_result=self.test_result,
            diff=self.diff,
            changed_files=self.changed_files,
            pending_command=self.pending_command,
            pending_approval_id=self.pending_approval_id,
            pending_risk=self.pending_risk,
            pending_reason=self.pending_reason,
            events=self.events,
            approvals=self.approvals,
            receipt=self.receipt,
            workspace_kind=self.workspace_kind,
            workspace_reason=self.workspace_reason,
            workspace_baseline=self.workspace_baseline,
            event_chain_head=chain_head or (self.events[-1].event_hash if self.events else ""),
            tool_calls=self.tool_calls,
            invalid_actions=self.invalid_actions,
            budget_used=self.budget_used,
            usage=self.usage,
            required_check_argv=self.required_check_argv,
            required_check_verified=self.required_check_verified,
            required_check_evidence_valid=self.required_check_evidence_valid,
            required_check_evidence_generation=self.required_check_evidence_generation,
            edit_generation=self.edit_generation,
            required_check_last_result=self.required_check_last_result,
            precondition_failures=self.precondition_failures,
        )


class TaskManager:
    def __init__(self, settings: Settings, *, store: SQLiteStore | None = None, runner: AgentRunner | None = None):
        self.settings = settings
        self.store = store or SQLiteStore(settings.database_path_resolved)
        self.records: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()
        self.store.recover_running_tasks()
        for row in self.store.list_tasks():
            self.records[row["id"]] = self._from_row(row)
        self.runner = runner or AgentRunner(settings, store=self.store)

    def _validate_repo(self, repo_path: str | None) -> Path:
        path = Path(repo_path or self.settings.repo_path_resolved).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"目标仓库不存在: {path}")
        patchproof_root = PATCHPROOF_ROOT.resolve()
        if (path == patchproof_root or patchproof_root in path.parents) and not self.settings.allow_project_target:
            safe_fixture_root = (patchproof_root / "benchmarks" / "fixtures").resolve()
            if path == safe_fixture_root or safe_fixture_root not in path.parents:
                raise ValueError("PatchProof 不能把自己的运行目录作为目标仓库")
        return path

    async def create(
        self,
        goal: str,
        repo_path: str | None,
        check_command: str | None,
        max_iterations: int | None,
        max_steps: int | None = None,
    ) -> TaskRecord:
        path = self._validate_repo(repo_path)
        task_id = uuid.uuid4().hex[:12]
        normalized_check_command = check_command or "python -m pytest -q"
        try:
            required_check_argv = normalize_command(normalized_check_command)
        except ValueError as exc:
            raise ValueError(f"check_command 无法解析: {exc}") from exc
        record = TaskRecord(
            id=task_id,
            goal=goal,
            repo_path=str(path),
            check_command=normalized_check_command,
            max_iterations=max_iterations or self.settings.max_iterations,
            max_steps=max_steps or self.settings.max_tool_steps,
            required_check_argv=required_check_argv,
        )
        record.approval_requester = self._create_approval
        self.store.create_task(
            task_id=record.id,
            goal=record.goal,
            repo_path=record.repo_path,
            check_command=record.check_command,
            max_iterations=record.max_iterations,
            max_steps=record.max_steps,
            required_check_argv=record.required_check_argv,
        )
        async with self._lock:
            self.records[task_id] = record
        await self._emit(record, "queued", "任务已持久化并加入队列", {"max_steps": record.max_steps})
        record.task = asyncio.create_task(self._run(record))
        return record

    async def _run(self, record: TaskRecord) -> None:
        try:
            await self.runner.run(record, lambda stage, message, data: self._emit(record, stage, message, data))
        except asyncio.CancelledError:
            if record.status != TaskStatus.CANCELLED:
                record.status = TaskStatus.CANCELLED
                await self._emit(record, TaskStatus.CANCELLED.value, "任务已取消", {})
        finally:
            if record.status not in TERMINAL_STATUSES and record.status != TaskStatus.AWAITING_COMMAND_APPROVAL:
                record.status = TaskStatus.FAILED_RECOVERABLE
                record.failure_category = record.failure_category or "runner_stopped"
                record.error = record.error or "任务执行器停止，状态可恢复但未形成完成证据"
                await self._emit(record, TaskStatus.FAILED_RECOVERABLE.value, record.error, {})
            self._persist(record)

    async def _emit(self, record: TaskRecord, stage: str, message: str, data: dict[str, Any]) -> TaskEvent:
        if stage in {item.value for item in TaskStatus}:
            record.status = TaskStatus(stage)
        record.current_stage = stage
        record.updated_at = _now()
        event = self.store.append_event(record.id, stage=stage, message=message, data=data)
        record.events.append(event)
        self._persist(record)
        return event

    def _persist(self, record: TaskRecord) -> None:
        self.store.update_task(
            record.id,
            status=record.status.value,
            current_stage=record.current_stage,
            iteration=record.iteration,
            max_iterations=record.max_iterations,
            max_steps=record.max_steps,
            updated_at=record.updated_at.astimezone(UTC).isoformat(),
            error=record.error,
            failure_category=record.failure_category,
            plan_json=json_dumps(record.plan) if record.plan is not None else None,
            test_result_json=json_dumps(record.test_result) if record.test_result is not None else None,
            diff=record.diff,
            changed_files_json=json_dumps(record.changed_files),
            pending_command_json=json_dumps(record.pending_command) if record.pending_command else None,
            pending_approval_id=record.pending_approval_id,
            pending_risk=record.pending_risk,
            pending_reason=record.pending_reason,
            workspace_kind=record.workspace_kind,
            workspace_reason=record.workspace_reason,
            workspace_baseline_json=json_dumps(record.workspace_baseline),
            tool_calls=record.tool_calls,
            invalid_actions=record.invalid_actions,
            budget_used=record.budget_used,
            usage_json=json_dumps(record.usage),
            required_check_argv_json=json_dumps(record.required_check_argv),
            required_check_verified=int(record.required_check_verified),
            required_check_evidence_generation=record.required_check_evidence_generation,
            edit_generation=record.edit_generation,
            required_check_last_result_json=(
                json_dumps(record.required_check_last_result) if record.required_check_last_result is not None else None
            ),
            precondition_failures=record.precondition_failures,
        )

    def _from_row(self, row: Any) -> TaskRecord:
        task_id = row["id"]
        receipt = self.store.get_receipt(task_id)
        if receipt:
            receipt.verified = verify_receipt(receipt.receipt, receipt.receipt_hash) and (
                receipt.file_verified or receipt.artifact_path is None
            )
        required_check_argv = json_loads(row["required_check_argv_json"], [])
        if not required_check_argv:
            try:
                required_check_argv = normalize_command(row["check_command"])
            except ValueError:
                required_check_argv = []
        record = TaskRecord(
            id=task_id,
            goal=row["goal"],
            repo_path=row["repo_path"],
            check_command=row["check_command"],
            max_iterations=row["max_iterations"],
            max_steps=row["max_steps"],
            status=TaskStatus(row["status"]),
            current_stage=row["current_stage"],
            iteration=row["iteration"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
            error=row["error"],
            failure_category=row["failure_category"],
            plan=json_loads(row["plan_json"], None),
            test_result=json_loads(row["test_result_json"], None),
            diff=row["diff"] or "",
            changed_files=json_loads(row["changed_files_json"], []),
            pending_command=json_loads(row["pending_command_json"], None),
            pending_approval_id=row["pending_approval_id"],
            pending_risk=row["pending_risk"],
            pending_reason=row["pending_reason"],
            events=self.store.get_events(task_id),
            approvals=self.store.get_approvals(task_id),
            receipt=receipt,
            workspace_kind=row["workspace_kind"],
            workspace_reason=row["workspace_reason"],
            workspace_baseline=json_loads(row["workspace_baseline_json"], {}),
            tool_calls=row["tool_calls"],
            invalid_actions=row["invalid_actions"],
            budget_used=row["budget_used"],
            usage=json_loads(row["usage_json"], {}),
            required_check_argv=required_check_argv,
            required_check_verified=bool(row["required_check_verified"]),
            required_check_evidence_generation=row["required_check_evidence_generation"],
            edit_generation=int(row["edit_generation"] or 0),
            required_check_last_result=json_loads(row["required_check_last_result_json"], None),
            precondition_failures=int(row["precondition_failures"] or 0),
        )
        record.approval_requester = self._create_approval
        return record

    def get(self, task_id: str) -> TaskRecord:
        record = self.records.get(task_id)
        if record is None:
            row = self.store.get_task(task_id)
            if row is None:
                raise KeyError(task_id)
            record = self._from_row(row)
            self.records[task_id] = record
        else:
            # Refresh the receipt artifact status so a later API/UI read can
            # expose file tampering even when the task record is cached.
            refreshed_receipt = self.store.get_receipt(task_id)
            if refreshed_receipt is not None:
                refreshed_receipt.verified = verify_receipt(
                    refreshed_receipt.receipt,
                    refreshed_receipt.receipt_hash,
                ) and refreshed_receipt.file_verified
                record.receipt = refreshed_receipt
        return record

    def list(self) -> list[TaskRecord]:
        result = []
        for row in self.store.list_tasks():
            result.append(self.get(row["id"]))
        return result

    async def _create_approval(self, record: TaskRecord, decision: CommandDecision) -> ApprovalSnapshot:
        approval = self.store.create_approval(
            record.id,
            kind="run_check",
            argv=list(decision.argv),
            risk_level=decision.risk_level,
            reason=decision.reason,
        )
        record.approvals.append(approval)
        record.pending_command = list(decision.argv)
        record.pending_approval_id = approval.id
        record.pending_risk = decision.risk_level
        record.pending_reason = decision.reason
        record.approval_waiters[approval.id] = asyncio.Event()
        self._persist(record)
        return approval

    async def approve_command(self, task_id: str, approved: bool, approval_id: str | None = None) -> TaskRecord:
        record = self.get(task_id)
        if record.status != TaskStatus.AWAITING_COMMAND_APPROVAL:
            raise ValueError("当前任务没有等待命令审批")
        current_id = approval_id or record.pending_approval_id
        if not current_id or current_id != record.pending_approval_id:
            raise ValueError("审批请求已变化，请使用当前 approval_id")
        approval = self.store.get_approval(current_id)
        if approval is None or approval.approved is not None:
            raise ValueError("审批请求不存在或已经处理")
        event = await self._emit(
            record,
            "approval_resolved",
            "命令审批已处理",
            {"approval_id": current_id, "approved": approved, "argv": approval.argv, "risk_level": approval.risk_level},
        )
        approval = self.store.resolve_approval(current_id, approved, event.seq)
        record.approvals = [approval if item.id == current_id else item for item in record.approvals]
        record.approval_results[current_id] = approved
        record.pending_command = None
        record.pending_approval_id = None
        record.pending_risk = None
        record.pending_reason = None
        record.approval_waiters.setdefault(current_id, asyncio.Event()).set()
        self._persist(record)
        return record

    async def apply(self, task_id: str) -> TaskRecord:
        record = self.get(task_id)
        if record.status != TaskStatus.AWAITING_APPLY:
            raise ValueError("只有等待写回的任务可以 Apply")
        if not record.workspace_kind:
            raise RuntimeError("任务缺少 workspace 类型，拒绝 Apply")
        original = Path(record.repo_path)
        staging = PATCHPROOF_ROOT / "data" / "runs" / record.id / "repo"
        workspace = open_workspace(original, staging, record.workspace_kind, self.settings.max_file_bytes)
        try:
            workspace.open_existing()
            changed = workspace.apply()
            record.status = TaskStatus.COMPLETED
            record.current_stage = TaskStatus.COMPLETED.value
            record.error = None
            record.failure_category = None
            record.updated_at = _now()
            await self._emit(
                record,
                TaskStatus.COMPLETED.value,
                "人工确认后已安全写回真实仓库",
                {"changed_files": changed},
            )
            if record.receipt:
                receipt = dict(record.receipt.receipt)
                receipt["verdict"] = "applied"
                receipt["applied_at"] = _now().isoformat()
                receipt["event_chain_head"] = self.store.chain_head(record.id)
                record.receipt = self._seal_and_store_receipt(record, receipt)
            if self.settings.cleanup_workspaces:
                workspace.cleanup()
            self._persist(record)
            return record
        except Exception as exc:
            record.error = str(exc)
            record.failure_category = "stale_apply" if "变化" in str(exc) else "apply_rejected"
            await self._emit(
                record,
                TaskStatus.FAILED_RECOVERABLE.value,
                "Apply 被拒绝，真实仓库未被覆盖",
                {"error": str(exc)},
            )
            raise

    async def cancel(self, task_id: str) -> TaskRecord:
        record = self.get(task_id)
        if record.status in TERMINAL_STATUSES:
            return record
        record.cancel_event.set()
        record.status = TaskStatus.CANCELLED
        record.current_stage = TaskStatus.CANCELLED.value
        record.updated_at = _now()
        await self._emit(record, TaskStatus.CANCELLED.value, "任务已取消", {})
        if record.task and not record.task.done():
            record.task.cancel()
        return record

    def verify_chain(self, task_id: str) -> bool:
        self.get(task_id)
        return self.store.verify_chain(task_id)

    def _seal_and_store_receipt(self, record: TaskRecord, payload: dict[str, Any]) -> ReceiptSnapshot:
        sealed = seal_receipt(payload)
        path, file_hash = write_receipt_atomic(record.id, sealed)
        self.store.save_artifact(
            record.id,
            kind="patch_receipt",
            path=str(path),
            sha256=file_hash,
            metadata={
                "schema": sealed.get("schema_version"),
                "receipt_hash": sealed["receipt_hash"],
                "canonical": True,
            },
        )
        snapshot = self.store.save_receipt(record.id, sealed)
        snapshot.verified = verify_receipt(sealed, snapshot.receipt_hash) and snapshot.file_verified
        return snapshot
