"""任务状态机、typed 动作与 API 快照 —— 任务域的数据契约。

做什么
------
定义任务生命周期需要的全部数据结构：状态机取值（TaskStatus）、持久化/展示用快照
（TaskSnapshot / TaskEvent / ApprovalSnapshot）、以及模型唯一能调用的 6 种 typed action。

怎么实现
--------
- 状态机：TaskStatus 是 StrEnum，字符串值就是 SQLite 里的 status 列，可直接落库。
- 动作契约：每个 ``*Action`` 都是 ``StrictModel``（extra="forbid", strict=True），
  由 agent/tools.py 的 TypeAdapter 按 ``tool`` 字段判别解析成具体类型。
- 完成态集合：TERMINAL_STATUSES / RUNNING_STATUSES 决定"还能不能继续推进 / 是否算完成"。

为什么
------
- 模型输出先过这个最严格的 schema：多塞字段、类型不匹配都会解析失败，从而保证
  "模型永远只能走白名单动作、永远不能盲写"。
- apply_edit 强制要求 expected_sha256 或 old_text 前置条件 —— 这是证据链的第一道闸：
  没有精确的旧内容或文件哈希，任何"编辑"都被拒绝。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    INSPECTING = "inspecting"
    PLANNING = "planning"
    EDITING = "editing"
    TESTING = "testing"
    REPAIRING = "repairing"
    AWAITING_COMMAND_APPROVAL = "awaiting_command_approval"
    AWAITING_APPLY = "awaiting_apply"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_RECOVERABLE = "failed_recoverable"
    CANCELLED = "cancelled"


# 终态：任务走到这些状态后不再自动推进。awaiting_apply 是"等人类裁决"的暂停态，
# completed 是写回后的终局，其余是失败/取消。进程重启时 running 态会转成 interrupted，
# 而绝不会被悄悄标成 completed —— 这是"没有完成就不能伪装成完成"的语义来源。
TERMINAL_STATUSES = {
    TaskStatus.AWAITING_APPLY,
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.FAILED_RECOVERABLE,
    TaskStatus.CANCELLED,
}


class ProviderOverride(BaseModel):
    """Per-task LLM provider override so a shared deployment can let each user
    bring their own API key.

    ``api_key`` is only ever held in memory on the TaskRecord; it is never
    persisted to SQLite, serialized to logs, or included in snapshots.
    """

    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    transport: Literal["auto", "anthropic-compatible", "openai-compatible"] | None = Field(
        default=None,
        description="Explicit transport. Required for custom OpenAI-compatible endpoints "
        "whose model name does not imply the transport.",
    )

    def masked(self) -> dict[str, str | None]:
        """Non-secret view: key is replaced by a fixed marker."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "transport": self.transport,
            "api_key": "***configured***" if self.api_key else None,
        }


class TaskCreate(BaseModel):
    goal: str = Field(min_length=8, max_length=2000)
    repo_path: str | None = None
    check_command: str | None = Field(default=None, max_length=500)
    max_iterations: int | None = Field(default=None, ge=1, le=20)
    max_steps: int | None = Field(default=None, ge=1, le=200)
    provider: ProviderOverride | None = None


class ApprovalRequest(BaseModel):
    approved: bool
    approval_id: str | None = Field(default=None, min_length=6, max_length=80)


class CommandRequest(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=32)


class TaskEvent(BaseModel):
    seq: int
    ts: datetime = Field(default_factory=utc_now)
    stage: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = ""
    event_hash: str = ""
    canonical_payload: str = ""


class ApprovalSnapshot(BaseModel):
    id: str
    task_id: str
    kind: str
    argv: list[str] = Field(default_factory=list)
    risk_level: str
    reason: str
    requested_at: datetime
    resolved_at: datetime | None = None
    approved: bool | None = None
    event_seq: int | None = None


class ReceiptSnapshot(BaseModel):
    receipt_hash: str
    receipt: dict[str, Any]
    verified: bool = False
    artifact_path: str | None = None
    file_sha256: str | None = None
    file_verified: bool = False


class TaskSnapshot(BaseModel):
    id: str
    goal: str
    repo_path: str
    status: TaskStatus
    current_stage: str
    iteration: int
    max_iterations: int
    max_steps: int = 32
    check_command: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    failure_category: str | None = None
    plan: dict[str, Any] | None = None
    test_result: dict[str, Any] | None = None
    diff: str = ""
    changed_files: list[str] = Field(default_factory=list)
    pending_command: list[str] | None = None
    pending_approval_id: str | None = None
    pending_risk: str | None = None
    pending_reason: str | None = None
    events: list[TaskEvent] = Field(default_factory=list)
    approvals: list[ApprovalSnapshot] = Field(default_factory=list)
    receipt: ReceiptSnapshot | None = None
    workspace_kind: str | None = None
    workspace_reason: str | None = None
    workspace_baseline: dict[str, Any] = Field(default_factory=dict)
    event_chain_head: str = ""
    tool_calls: int = 0
    invalid_actions: int = 0
    budget_used: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    required_check_argv: list[str] = Field(default_factory=list)
    required_check_verified: bool = False
    required_check_evidence_valid: bool = False
    required_check_evidence_generation: int | None = None
    edit_generation: int = 0
    required_check_last_result: dict[str, Any] | None = None
    precondition_failures: int = 0
    provider: dict[str, str | None] | None = Field(
        default=None,
        description="Masked per-task provider override; api_key is never included.",
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchRepoAction(StrictModel):
    tool: Literal["search_repo"]
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=20, ge=1, le=100)


class ReadFileAction(StrictModel):
    tool: Literal["read_file"]
    path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(default=1, ge=1, le=100_000)
    end_line: int | None = Field(default=None, ge=1, le=100_000)


class ApplyEditAction(StrictModel):
    tool: Literal["apply_edit"]
    path: str = Field(min_length=1, max_length=500)
    new_text: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    old_text: str | None = Field(default=None, max_length=1_000_000)
    reason: str = Field(default="", max_length=1000)

    @field_validator("expected_sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        if value is not None and any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("expected_sha256 必须是十六进制 SHA-256")
        return value.lower() if value else value

    @model_validator(mode="after")
    def require_precondition(self) -> ApplyEditAction:
        # 证据前置：任何编辑都必须先证明"我知道文件现在长什么样"。
        # 没有 expected_sha256（文件哈希）或 old_text（精确旧片段）就拒绝，
        # 模型无法靠瞎猜乱改文件 —— 这是与事件链配套的第一道防盲写闸门。
        if self.expected_sha256 is None and self.old_text is None:
            raise ValueError("apply_edit 必须提供 expected_sha256 或 old_text 前置条件")
        return self


class InspectDiffAction(StrictModel):
    tool: Literal["inspect_diff"]


class RunCheckAction(StrictModel):
    tool: Literal["run_check"]
    argv: list[str] = Field(min_length=1, max_length=32)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class FinishAction(StrictModel):
    tool: Literal["finish"]
    summary: str = Field(min_length=1, max_length=2000)
    # verdict 只是"声明"，不等于"完成"。真正的完成资格由 runner 校验：
    # 必须是 verified，且 required_check 在最新 edit_generation 后按原 argv 成功。
    verdict: Literal["verified", "blocked", "incomplete"] = "verified"


ToolAction = SearchRepoAction | ReadFileAction | ApplyEditAction | InspectDiffAction | RunCheckAction | FinishAction
