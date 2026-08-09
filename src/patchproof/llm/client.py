"""LLM provider adapters with explicit transports and shared budget accounting."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Protocol

try:
    from anthropic import AsyncAnthropic
except ModuleNotFoundError:  # pragma: no cover - optional before installation
    AsyncAnthropic = None  # type: ignore[assignment,misc]

try:
    from openai import AsyncOpenAI
except ModuleNotFoundError:  # pragma: no cover - optional before installation
    AsyncOpenAI = None  # type: ignore[assignment,misc]

from ..agent.tools import tool_catalog
from ..config import Settings
from .budget import BudgetLedger


class LLMUnavailableError(RuntimeError):
    pass


class LLMTransportError(RuntimeError):
    """Concise provider/response failure safe for reports and logs."""

    def __init__(self, category: str, message: str, *, status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "message": str(self),
            "status_code": self.status_code,
        }


class LLMOutputTruncatedError(LLMTransportError):
    """Provider returned a successful response cut off by its token limit."""

    def __init__(self):
        super().__init__("provider_output_truncated", "provider output reached the configured token limit")


class AgentModel(Protocol):
    metadata: dict[str, Any]
    usage: dict[str, Any]

    async def plan(self, goal: str, index_context: str, source_context: str, check_command: str) -> dict[str, Any]: ...

    async def next_action(
        self,
        goal: str,
        plan: dict[str, Any],
        context: str,
        observations: Sequence[dict[str, Any]],
        step: int,
    ) -> dict[str, Any]: ...


class OneShotModel(Protocol):
    metadata: dict[str, Any]
    usage: dict[str, Any]

    async def one_shot(
        self,
        goal: str,
        index_context: str,
        source_context: str,
        check_command: str,
    ) -> dict[str, Any]: ...


class LLMClient:
    """Explicit Anthropic/OpenAI-compatible adapter with hard budget accounting."""

    def __init__(self, settings: Settings, *, ledger: BudgetLedger | None = None, client: Any | None = None):
        self.settings = settings
        self.ledger = ledger
        self.transport = settings.resolved_transport
        kwargs: dict[str, Any] = {
            "api_key": settings.anthropic_api_key,
            "timeout": settings.llm_timeout_seconds,
            "max_retries": settings.llm_max_retries,
        }
        if settings.resolved_base_url:
            kwargs["base_url"] = settings.resolved_base_url
        if client is not None:
            self.client = client
        elif not settings.llm_enabled:
            self.client = None
        else:
            try:
                if self.transport == "openai-compatible":
                    self.client = AsyncOpenAI(**kwargs) if AsyncOpenAI else None
                else:
                    self.client = AsyncAnthropic(**kwargs) if AsyncAnthropic else None
            except (TypeError, ValueError) as exc:
                raise LLMUnavailableError("provider client configuration is invalid") from exc
        self.metadata = dict(settings.provider_metadata)
        self.usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "requests": 0}

    async def json(self, system: str, prompt: str) -> dict[str, Any]:
        if self.client is None:
            if not self.settings.llm_enabled:
                raise LLMUnavailableError("当前配置没有可用的模型 API key")
            sdk = "openai" if self.transport == "openai-compatible" else "anthropic"
            raise LLMUnavailableError(f"当前 Python 环境没有安装 {sdk} SDK")
        reservation: str | None = None
        input_estimate = max(1, (len(system) + len(prompt)) // 4)
        if self.ledger is not None:
            reservation = self.ledger.reserve(
                input_tokens=input_estimate,
                requested_output_tokens=self.settings.anthropic_max_tokens,
            )
        try:
            response = await self._request(system, prompt)
        except Exception as exc:
            if reservation is not None:
                self.ledger.cancel(reservation)
            if isinstance(exc, LLMTransportError):
                raise
            status_code = getattr(exc, "status_code", None)
            status = int(status_code) if isinstance(status_code, int) else None
            suffix = f" (HTTP {status})" if status is not None else ""
            raise LLMTransportError(
                "provider_request_failed",
                f"{self.transport} provider request failed{suffix}",
                status_code=status,
            ) from exc

        response_usage = getattr(response, "usage", None)
        input_name = "prompt_tokens" if self.transport == "openai-compatible" else "input_tokens"
        output_name = "completion_tokens" if self.transport == "openai-compatible" else "output_tokens"
        raw_input = getattr(response_usage, input_name, None) if response_usage is not None else None
        raw_output = getattr(response_usage, output_name, None) if response_usage is not None else None
        try:
            text = self._response_text(response)
        except (AttributeError, IndexError, TypeError) as exc:
            text = ""
            response_error = LLMTransportError("invalid_provider_response", "provider response contained no text")
            response_error.__cause__ = exc
        else:
            response_error = None
        observed_input = int(raw_input) if raw_input is not None else input_estimate
        observed_output = int(raw_output) if raw_output is not None else (max(1, len(text) // 4) if text else 0)
        if reservation is not None:
            self.ledger.commit(reservation, input_tokens=observed_input, output_tokens=observed_output)
            self.usage["budget"] = self.ledger.snapshot()
        self.usage["requests"] = int(self.usage.get("requests", 0)) + 1
        self.usage["input_tokens"] = int(self.usage.get("input_tokens", 0)) + observed_input
        self.usage["output_tokens"] = int(self.usage.get("output_tokens", 0)) + observed_output
        self.usage["estimated_tokens"] = raw_input is None or raw_output is None
        if self._output_was_truncated(response):
            raise LLMOutputTruncatedError()
        if response_error is not None:
            raise response_error
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise LLMTransportError("invalid_json", "provider response did not contain a JSON object")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMTransportError(
                "invalid_json",
                f"provider JSON was invalid at line {exc.lineno}, column {exc.colno}",
            ) from exc
        if not isinstance(data, dict):
            raise LLMTransportError("invalid_json", "provider JSON top level was not an object")
        return data

    async def _request(self, system: str, prompt: str) -> Any:
        if self.transport == "openai-compatible":
            kwargs: dict[str, Any] = {
                "model": self.settings.anthropic_model,
                "max_tokens": self.settings.anthropic_max_tokens,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
            reasoning_effort = self._reasoning_effort()
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort
            return await self.client.chat.completions.create(**kwargs)
        return await self.client.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=self.settings.anthropic_max_tokens,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

    def _reasoning_effort(self) -> str | None:
        """Map the reasoning control to an OpenAI-compatible request parameter.

        deepseek-v4-flash on the OpenCode gateway defaults to extended
        reasoning that consumes the whole output budget before emitting any
        content, so every response truncates. ``off`` disables reasoning and
        ``on`` requests the explicit high-effort mode; ``auto`` leaves the
        provider default untouched.
        """
        return {"on": "high", "off": "none"}.get(self.settings.llm_reasoning)

    def _response_text(self, response: Any) -> str:
        if self.transport == "openai-compatible":
            content = response.choices[0].message.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(getattr(item, "text", ""))
                    for item in content
                )
            raise TypeError("OpenAI response content is not text")
        return "".join(getattr(block, "text", "") for block in (response.content or []))

    def _output_was_truncated(self, response: Any) -> bool:
        if self.transport == "openai-compatible":
            choices = getattr(response, "choices", None) or []
            reason = getattr(choices[0], "finish_reason", None) if choices else None
            return reason in {"length", "max_tokens"}
        return getattr(response, "stop_reason", None) in {"max_tokens", "length"}

    async def plan(self, goal: str, index_context: str, source_context: str, check_command: str) -> dict[str, Any]:
        prompt = (
            f"任务目标:\n{goal}\n\n"
            f"默认验证命令（需要时请通过 run_check 使用 argv）:\n{check_command}\n\n"
            f"仓库索引:\n{index_context}\n\n"
            f"相关源码:\n{source_context}\n\n"
            '只返回 JSON: {"summary":"...","steps":["..."],"checks":["..."]}。'
        )
        return await self.json(
            "你是 Evidence-first Coding Agent 的计划器。只基于仓库证据规划，不能编造文件、API 或测试。",
            prompt,
        )

    async def next_action(
        self,
        goal: str,
        plan: dict[str, Any],
        context: str,
        observations: Sequence[dict[str, Any]],
        step: int,
    ) -> dict[str, Any]:
        prompt = (
            f"目标:\n{goal}\n\n"
            f"计划:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
            f"仓库上下文:\n{context}\n\n"
            f"第 {step} 步之前的工具观察:\n{json.dumps(list(observations)[-8:], ensure_ascii=False)}\n\n"
            f"可用 typed tools:\n{json.dumps(tool_catalog(), ensure_ascii=False)}\n\n"
            "一次只返回一个 JSON action 对象，不要返回 edits 列表，不要调用未列出的工具。"
            "action 必须含 tool 字段，键名必须与工具目录一致，例如："
            '{"tool":"read_file","path":"pysnooper/tracer.py","start_line":1,"end_line":60}；'
            '{"tool":"apply_edit","path":"pysnooper/tracer.py","old_text":"精确旧片段",'
            '"new_text":"新片段","reason":"修复 unicode"}；'
            '{"tool":"run_check","argv":["pytest","-q","tests/test_chinese.py::test_chinese"]}。'
            "apply_edit 必须提供 expected_sha256 或 old_text。"
            "必须先通过 run_check 获得成功测试，再返回 finish(verdict=verified)。"
        )
        return await self.json(
            "你是保守的仓库维护 Agent。每一步都必须可解释、可回放、可验证，路径只能是仓库相对路径。",
            prompt,
        )

    async def one_shot(
        self,
        goal: str,
        index_context: str,
        source_context: str,
        check_command: str,
    ) -> dict[str, Any]:
        prompt = (
            f"任务目标:\n{goal}\n\n"
            f"验证命令:\n{check_command}\n\n"
            f"仓库索引:\n{index_context}\n\n"
            f"相关源码:\n{source_context}\n\n"
            '只返回 JSON，键名必须完全一致，例如：'
            '{"summary":"修复 unicode 输出","edits":'
            "[{\"path\":\"pysnooper/tracer.py\",\"old_text\":\"        encoding = 'ascii'\\n\","
            "\"new_text\":\"        encoding = 'utf-8'\\n\","
            "\"expected_sha256\":\"<可选：当前文件64位SHA-256>\"}]}。\n"
            "默认必须使用紧凑 old_text/new_text 替换。old_text 必须从上面的「相关源码」中逐字符复制"
            "（含缩进和引号），禁止发明、重构或改写代码；new_text 必须和 old_text 不同。"
            "如果无法从「相关源码」里找到要修改的精确唯一片段，就不要猜 old_text，"
            "改为以 new_text 提供完整文件并省略 old_text。最多 20 个编辑。"
            "不要使用 expected_contents、不要调用工具、不要修改敏感配置文件。"
        )
        return await self.json(
            "你是 one-shot coding baseline。只能一次性返回紧凑编辑 JSON，不能使用工具或获得迭代反馈；"
            "结果会在隔离副本中通过同一验证命令检查。",
            prompt,
        )


class FakeLLM:
    """Deterministic dependency-injection model for tests and smoke benchmarks."""

    def __init__(
        self,
        actions: Sequence[dict[str, Any]],
        plan: dict[str, Any] | None = None,
        one_shot_result: dict[str, Any] | None = None,
        ledger: BudgetLedger | None = None,
    ):
        self.actions = list(actions)
        self.plan_result = plan or {"summary": "deterministic smoke plan", "steps": [], "checks": []}
        self.one_shot_result = one_shot_result or {"summary": "deterministic one-shot result", "edits": []}
        self.ledger = ledger
        self.metadata = {"provider": "fake", "model": "deterministic-smoke"}
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}

    async def plan(self, goal: str, index_context: str, source_context: str, check_command: str) -> dict[str, Any]:
        self._consume_budget(goal, index_context, requested_output_tokens=1)
        self.usage["requests"] += 1
        return self.plan_result

    async def next_action(
        self,
        goal: str,
        plan: dict[str, Any],
        context: str,
        observations: Sequence[dict[str, Any]],
        step: int,
    ) -> dict[str, Any]:
        self._consume_budget(goal, context, requested_output_tokens=1)
        self.usage["requests"] += 1
        if self.actions:
            return self.actions.pop(0)
        return {"tool": "finish", "summary": "fake action budget exhausted", "verdict": "blocked"}

    async def one_shot(
        self,
        goal: str,
        index_context: str,
        source_context: str,
        check_command: str,
    ) -> dict[str, Any]:
        self._consume_budget(goal, source_context, requested_output_tokens=1)
        self.usage["requests"] += 1
        return self.one_shot_result

    def _consume_budget(self, *parts: str, requested_output_tokens: int) -> None:
        if self.ledger is None:
            return
        request_id = self.ledger.reserve(
            input_tokens=max(1, sum(len(part) for part in parts) // 4),
            requested_output_tokens=requested_output_tokens,
        )
        self.ledger.commit(request_id, input_tokens=max(1, sum(len(part) for part in parts) // 4), output_tokens=0)
        self.usage["budget"] = self.ledger.snapshot()
