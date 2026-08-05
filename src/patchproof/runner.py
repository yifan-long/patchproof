from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_tools import InvalidToolAction, observation, parse_tool_action
from .budget import BudgetExceeded
from .config import PATCHPROOF_ROOT, Settings
from .llm import AgentModel, LLMClient, LLMUnavailableError
from .models import (
    ApplyEditAction,
    FinishAction,
    InspectDiffAction,
    ReadFileAction,
    RunCheckAction,
    SearchRepoAction,
    TaskStatus,
)
from .policy import ProcessExecutor, classify_argv, parse_command
from .receipt import build_patch_receipt, verify_receipt, write_receipt_atomic
from .repo_index import RepoIndex
from .storage import SQLiteStore
from .workspace import WorkspacePreconditionError, WorkspaceProtocol, select_workspace

Emit = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


class AgentRunner:
    """Bounded typed tool loop with explicit observations and evidence."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: SQLiteStore | None = None,
        llm: AgentModel | None = None,
        executor: ProcessExecutor | None = None,
        evaluation_context: dict[str, Any] | None = None,
    ):
        self.settings = settings
        self.store = store or SQLiteStore(settings.database_path_resolved)
        self.llm: AgentModel = llm or LLMClient(settings)
        self.executor = executor or ProcessExecutor(settings.max_output_chars)
        self.evaluation_context = evaluation_context

    async def run(self, record: Any, emit: Emit) -> None:
        original = Path(record.repo_path).resolve()
        staging = PATCHPROOF_ROOT / "data" / "runs" / record.id / "repo"
        workspace: WorkspaceProtocol | None = None
        record.started_at = record.started_at or datetime.now(UTC).isoformat()
        observations: list[dict[str, Any]] = []
        last_test_passed = False
        try:
            await emit("inspecting", "正在选择并创建隔离工作区", {})
            workspace = select_workspace(
                original,
                staging,
                self.settings.max_file_bytes,
                allow_git_worktree=self.settings.allow_git_worktree,
            )
            workspace.create()
            record.workspace_kind = workspace.kind
            record.workspace_reason = workspace.reason
            record.workspace_baseline = workspace.baseline
            await emit("workspace_selected", "隔离工作区已创建", workspace.metadata)

            index = RepoIndex.build(staging)
            index_context = (
                str(self.evaluation_context["index_context"])
                if self.evaluation_context and "index_context" in self.evaluation_context
                else index.context_for(record.goal)
            )
            source_context = (
                str(self.evaluation_context["source_context"])
                if self.evaluation_context and "source_context" in self.evaluation_context
                else index.source_context(record.goal)
            )
            await emit("inspecting", "仓库索引完成", {"index": index.summary(), "mode": workspace.kind})

            await emit("planning", "正在生成显式任务计划", {"model": self._model_metadata()})
            self._ensure_llm_budget(record)
            record.plan = await self.llm.plan(record.goal, index_context, source_context, record.check_command)
            record.usage = dict(getattr(self.llm, "usage", {}) or {})
            await emit(
                "planning",
                "任务计划已生成",
                {"plan": record.plan, "model": self._model_metadata(), "usage": record.usage},
            )

            context = f"{index_context}\n\n{source_context}"
            for step in range(1, record.max_steps + 1):
                if record.cancel_event.is_set():
                    raise asyncio.CancelledError
                record.budget_used = step
                self._ensure_llm_budget(record)
                await emit("editing", f"Tool loop 第 {step}/{record.max_steps} 步", {"step": step})
                raw_action = await self.llm.next_action(record.goal, record.plan or {}, context, observations, step)
                record.usage = dict(getattr(self.llm, "usage", {}) or {})
                try:
                    action = parse_tool_action(raw_action)
                except InvalidToolAction as exc:
                    record.invalid_actions += 1
                    invalid_observation = observation(
                        "invalid_action",
                        ok=False,
                        error=f"非法或不支持的 tool action: {exc.error}",
                        data={
                            "allowed_tools": [
                                "search_repo",
                                "read_file",
                                "apply_edit",
                                "inspect_diff",
                                "run_check",
                                "finish",
                            ]
                        },
                    )
                    observations.append(invalid_observation)
                    await emit(
                        "invalid_action",
                        "模型返回了非法 typed action，生成受限 observation",
                        invalid_observation,
                    )
                    if record.invalid_actions >= self.settings.max_invalid_actions:
                        raise AgentFailure("invalid_action_limit", "模型连续返回非法 action，停止执行")
                    continue

                record.tool_calls += 1
                action_data = action.model_dump(mode="json")
                await emit(
                    "tool_call",
                    f"调用 typed tool: {action.tool}",
                    {"step": step, "action": action_data, "model": self._model_metadata(), "usage": record.usage},
                )
                result, last_test_passed = await self._dispatch(
                    record,
                    workspace,
                    action,
                    emit,
                    last_test_passed,
                )
                observations.append(result)
                await emit("observation", f"收到 {action.tool} observation", result)
                record.diff, record.changed_files = workspace.diff()
                self._persist_artifact_stats(record, workspace)
                if isinstance(action, FinishAction) and result.get("ok"):
                    await self._create_receipt(record, workspace, record.required_check_evidence_valid)
                    await emit(
                        TaskStatus.AWAITING_APPLY.value,
                        "测试证据满足要求，已生成 Patch Receipt，等待人工 Apply",
                        {
                            "receipt_hash": record.receipt.receipt_hash if record.receipt else None,
                            "diff_hash": _hash(record.diff),
                            "event_chain_head": self.store.chain_head(record.id),
                        },
                    )
                    return

            raise AgentFailure("budget_exhausted", "达到 typed tool loop 最大步数，未形成完成证据")
        except asyncio.CancelledError:
            record.status = TaskStatus.CANCELLED
            await emit(TaskStatus.CANCELLED.value, "任务被取消", {})
        except LLMUnavailableError as exc:
            record.status = TaskStatus.FAILED
            record.failure_category = "llm_unavailable"
            record.error = str(exc)
            await emit(TaskStatus.FAILED.value, str(exc), {"failure_category": record.failure_category})
        except BudgetExceeded as exc:
            record.status = TaskStatus.FAILED
            record.failure_category = "llm_budget_exhausted"
            record.error = str(exc)
            await emit(
                TaskStatus.FAILED.value,
                str(exc),
                {"failure_category": record.failure_category, "budget": exc.snapshot},
            )
        except AgentFailure as exc:
            record.status = TaskStatus.FAILED
            record.failure_category = exc.category
            record.error = str(exc)
            await emit(TaskStatus.FAILED.value, str(exc), {"failure_category": exc.category})
        except Exception as exc:  # noqa: BLE001
            record.status = TaskStatus.FAILED
            record.failure_category = "runner_error"
            record.error = str(exc)
            await emit(TaskStatus.FAILED.value, str(exc), {"failure_category": record.failure_category})

    async def _dispatch(
        self,
        record: Any,
        workspace: WorkspaceProtocol,
        action: Any,
        emit: Emit,
        last_test_passed: bool,
    ) -> tuple[dict[str, Any], bool]:
        try:
            if isinstance(action, SearchRepoAction):
                return (
                    observation(action.tool, ok=True, data=workspace.search_repo(action.query, action.max_results)),
                    last_test_passed,
                )
            if isinstance(action, ReadFileAction):
                content = workspace.read_file(action.path, action.start_line, action.end_line)
                return (
                    observation(action.tool, ok=True, data={"path": action.path, "content": content}),
                    last_test_passed,
                )
            if isinstance(action, ApplyEditAction):
                edit = workspace.apply_edit(
                    action.path,
                    action.new_text,
                    expected_sha256=action.expected_sha256,
                    old_text=action.old_text,
                    reason=action.reason,
                )
                # A successful edit changes the state that a check observed.
                # No prior check, including the required check, may authorize
                # finish across this generation boundary.
                record.edit_generation += 1
                record.required_check_verified = False
                record.required_check_evidence_generation = None
                record.required_check_last_result = {
                    "invalidated_by_edit": True,
                    "edit_generation": record.edit_generation,
                    "path": action.path,
                }
                return observation(action.tool, ok=True, data=edit), False
            if isinstance(action, InspectDiffAction):
                diff, changed = workspace.diff()
                return observation(
                    action.tool,
                    ok=True,
                    data={"diff": diff, "changed_files": changed, "diff_hash": _hash(diff)},
                ), last_test_passed
            if isinstance(action, RunCheckAction):
                result = await self._run_check(record, workspace, action, emit)
                return (
                    observation(action.tool, ok=result.get("returncode") == 0, data=result),
                    record.required_check_evidence_valid,
                )
            if isinstance(action, FinishAction):
                if action.verdict != "verified":
                    return (
                        observation(action.tool, ok=False, error=f"finish verdict={action.verdict} 不能声明完成"),
                        last_test_passed,
                    )
                if not record.required_check_evidence_valid:
                    return (
                        observation(
                            action.tool,
                            ok=False,
                            error=(
                                "没有在最近一次编辑后成功执行与 required check 完全一致的 run_check，"
                                "拒绝进入 awaiting_apply"
                            ),
                            data={
                                "required_check_argv": record.required_check_argv,
                                "evidence_generation": record.required_check_evidence_generation,
                                "edit_generation": record.edit_generation,
                            },
                        ),
                        last_test_passed,
                    )
                return (
                    observation(action.tool, ok=True, data={"summary": action.summary, "verdict": action.verdict}),
                    last_test_passed,
                )
            return observation("unknown", ok=False, error="dispatcher 未注册该 typed tool"), last_test_passed
        except AgentFailure:
            raise
        except (ValueError, RuntimeError, OSError) as exc:
            if isinstance(exc, WorkspacePreconditionError):
                record.precondition_failures += 1
            next_test_passed = False if isinstance(action, (ApplyEditAction, RunCheckAction)) else last_test_passed
            return observation(getattr(action, "tool", "unknown"), ok=False, error=str(exc)), next_test_passed

    async def _run_check(
        self,
        record: Any,
        workspace: WorkspaceProtocol,
        action: RunCheckAction,
        emit: Emit,
    ) -> dict[str, Any]:
        if record.iteration >= record.max_iterations:
            raise AgentFailure("iteration_limit", "达到最大验证/修复轮数")
        spec = parse_command(action.argv)
        normalized_argv = spec.argv
        decision = classify_argv(normalized_argv)
        approval_id: str | None = None
        approved = True
        if decision.requires_approval:
            approval = await record.request_approval(decision)
            approval_id = approval.id
            await emit(
                TaskStatus.AWAITING_COMMAND_APPROVAL.value,
                "检查命令需要人工审批，任务已暂停",
                {
                    "approval_id": approval.id,
                    "argv": list(decision.argv),
                    "risk_level": decision.risk_level,
                    "reason": decision.reason,
                },
            )
            approved = await record.wait_for_approval(approval.id)
            if not approved:
                command_record = {
                    "argv": list(decision.argv),
                    "risk_level": decision.risk_level,
                    "reason": decision.reason,
                    "approval_id": approval.id,
                    "approved": False,
                    "returncode": None,
                    "required_check_match": list(decision.argv) == record.required_check_argv,
                    "evidence_generation": record.required_check_evidence_generation,
                }
                record.commands.append(command_record)
                return {"argv": list(decision.argv), "returncode": None, "approved": False, "reason": "人工拒绝命令"}
            await emit(
                "testing",
                "人工批准检查命令，继续执行",
                {"approval_id": approval.id, "argv": list(decision.argv)},
            )
        record.iteration += 1
        result = await self.executor.run(
            spec,
            cwd=str(workspace.staging),
            timeout_seconds=action.timeout_seconds or self.settings.command_timeout_seconds,
            cancel_event=record.cancel_event,
        )
        result_data = result.as_dict()
        result_data.update(
            {
                "risk_level": decision.risk_level,
                "policy_reason": decision.reason,
                "approval_id": approval_id,
                "approved": approved,
                "required_check_argv": record.required_check_argv,
                "required_check_match": normalized_argv == record.required_check_argv,
                "edit_generation": record.edit_generation,
            }
        )
        required_match = normalized_argv == record.required_check_argv
        if result.returncode == 0 and required_match:
            record.required_check_verified = True
            record.required_check_evidence_generation = record.edit_generation
        record.required_check_last_result = {
            "argv": normalized_argv,
            "returncode": result.returncode,
            "required_match": required_match,
            "verified": record.required_check_evidence_valid,
            "edit_generation": record.edit_generation,
        }
        result_data["required_check_verified"] = record.required_check_evidence_valid
        result_data["required_check_evidence_generation"] = record.required_check_evidence_generation
        record.commands.append(result_data)
        record.test_result = result_data
        await emit("testing", "验证命令执行完成", result_data)
        if result.returncode != 0 and record.iteration < record.max_iterations:
            await emit("repairing", "验证失败，失败 observation 将驱动下一轮修复", {"iteration": record.iteration})
        return result_data

    async def _create_receipt(self, record: Any, workspace: WorkspaceProtocol, test_passed: bool) -> None:
        diff, changed = workspace.diff()
        record.diff = diff
        record.changed_files = changed
        record.usage = dict(getattr(self.llm, "usage", {}) or {})
        receipt = build_patch_receipt(
            task_id=record.id,
            goal=record.goal,
            workspace=workspace.metadata,
            model=self._model_metadata(),
            plan=record.plan,
            tool_stats={
                "tool_calls": record.tool_calls,
                "invalid_actions": record.invalid_actions,
                "budget_used": record.budget_used,
            },
            changed_files=workspace.change_records(),
            diff_hash=_hash(diff),
            commands=record.commands,
            approvals=[item.model_dump(mode="json") for item in record.approvals],
            tests={
                "passed": test_passed,
                "last": record.test_result or {},
                "iterations": record.iteration,
                "required_check": {
                    "argv": record.required_check_argv,
                    "verified": record.required_check_evidence_valid,
                    "evidence_generation": record.required_check_evidence_generation,
                    "edit_generation": record.edit_generation,
                    "last_result": record.required_check_last_result,
                },
            },
            event_chain_head=self.store.chain_head(record.id),
            started_at=record.started_at or datetime.now(UTC).isoformat(),
            ended_at=datetime.now(UTC).isoformat(),
            verdict="verified_pending_apply",
        )
        path, file_hash = write_receipt_atomic(record.id, receipt)
        self.store.save_artifact(
            record.id,
            kind="patch_receipt",
            path=str(path),
            sha256=file_hash,
            metadata={
                "schema": receipt.get("schema_version"),
                "receipt_hash": receipt["receipt_hash"],
                "canonical": True,
            },
        )
        record.receipt = self.store.save_receipt(record.id, receipt)
        record.receipt.verified = verify_receipt(receipt, record.receipt.receipt_hash) and record.receipt.file_verified

    def _persist_artifact_stats(self, record: Any, workspace: WorkspaceProtocol) -> None:
        record.diff, record.changed_files = workspace.diff()
        record.usage = dict(getattr(self.llm, "usage", {}) or {})

    def _model_metadata(self) -> dict[str, Any]:
        return dict(getattr(self.llm, "metadata", {}) or {})

    def _ensure_llm_budget(self, record: Any) -> None:
        usage = getattr(self.llm, "usage", {}) or {}
        if int(usage.get("requests", 0)) >= self.settings.max_llm_calls:
            raise AgentFailure("llm_budget_exhausted", "达到模型调用预算，停止执行")


class AgentFailureError(RuntimeError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


AgentFailure = AgentFailureError


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
