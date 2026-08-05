"""Typed action schema and explicit dispatcher helpers for the agent loop."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, TypeAdapter, ValidationError

from .models import (
    ApplyEditAction,
    FinishAction,
    InspectDiffAction,
    ReadFileAction,
    RunCheckAction,
    SearchRepoAction,
    ToolAction,
)

TOOL_NAMES = (
    "search_repo",
    "read_file",
    "apply_edit",
    "inspect_diff",
    "run_check",
    "finish",
)

TOOL_ACTION_ADAPTER = TypeAdapter(
    Annotated[
        SearchRepoAction | ReadFileAction | ApplyEditAction | InspectDiffAction | RunCheckAction | FinishAction,
        Field(discriminator="tool"),
    ]
)


class InvalidToolActionError(ValueError):
    def __init__(self, payload: Any, error: str):
        self.payload = payload
        self.error = error
        super().__init__(error)


def parse_tool_action(payload: Any) -> ToolAction:
    candidate = payload.get("action", payload) if isinstance(payload, dict) else payload
    try:
        return TOOL_ACTION_ADAPTER.validate_python(candidate)
    except ValidationError as exc:
        raise InvalidToolActionError(candidate, str(exc)) from exc


InvalidToolAction = InvalidToolActionError


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_repo",
            "input": {"query": "string", "max_results": "integer 1..100"},
            "purpose": "在隔离工作区中搜索代码和符号引用",
        },
        {
            "name": "read_file",
            "input": {"path": "relative string", "start_line": "integer", "end_line": "integer?"},
            "purpose": "读取有限范围的 UTF-8 文本",
        },
        {
            "name": "apply_edit",
            "input": {
                "path": "relative string",
                "new_text": "string",
                "expected_sha256": "sha256?",
                "old_text": "string?",
                "reason": "string?",
            },
            "purpose": "在前置 hash 或 old_text 校验后原子编辑一个文件",
        },
        {"name": "inspect_diff", "input": {}, "purpose": "查看当前隔离工作区 diff"},
        {
            "name": "run_check",
            "input": {"argv": "string array", "timeout_seconds": "integer?"},
            "purpose": "经过策略引擎运行一个检查命令；非白名单命令会暂停审批",
        },
        {
            "name": "finish",
            "input": {"summary": "string", "verdict": "verified|blocked|incomplete"},
            "purpose": "声明结果；只有有成功测试证据时才能进入 awaiting_apply",
        },
    ]


def observation(tool: str, *, ok: bool, data: Any = None, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool": tool, "ok": ok}
    if data is not None:
        payload["data"] = data
    if error is not None:
        payload["error"] = error
    return payload
