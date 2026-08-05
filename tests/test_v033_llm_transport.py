from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from patchproof.budget import BudgetLedger, BudgetLimits
from patchproof.config import Settings
from patchproof.llm import LLMClient, LLMOutputTruncatedError, LLMTransportError


class FakeCreate:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeOpenAI:
    def __init__(self, create: FakeCreate):
        self.chat = SimpleNamespace(completions=create)


class FakeAnthropic:
    def __init__(self, create: FakeCreate):
        self.messages = create


def _settings(tmp_path: Path, **values) -> Settings:
    return Settings(
        env_file_path=str(tmp_path / "missing.env"),
        profile_file_path=str(tmp_path / "missing-profile.env"),
        anthropic_api_key="test-only-key",
        anthropic_max_tokens=32,
        **values,
    )


def _ledger() -> BudgetLedger:
    return BudgetLedger(
        BudgetLimits(
            max_requests=4,
            max_input_tokens=100,
            max_output_tokens=100,
            max_cost_usd=1,
            reserve_output_tokens=32,
        )
    )


def test_deepseek_root_uses_archived_zen_profile_without_secret(tmp_path: Path, monkeypatch) -> None:
    secret = "never-expose-this"
    source = tmp_path / "deepseek.env"
    source.write_text(
        f"DEEPSEEK_API_KEY={secret}\n"
        "DEEPSEEK_BASE_URL=https://opencode.ai\n"
        "DEEPSEEK_MODEL=deepseek-v4-flash\n"
        "OPENCODE_PLAN=zen\n",
        encoding="utf-8",
    )
    for name in (
        "PATCHPROOF_ANTHROPIC_API_KEY",
        "PATCHPROOF_ANTHROPIC_BASE_URL",
        "PATCHPROOF_ANTHROPIC_MODEL",
        "PATCHPROOF_LLM_PROVIDER",
        "PATCHPROOF_LLM_TRANSPORT",
        "PATCHPROOF_OPENCODE_PLAN",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=str(source), profile_file_path=str(tmp_path / "missing-profile.env"))

    assert settings.resolved_provider == "deepseek"
    assert settings.resolved_transport == "openai-compatible"
    assert settings.resolved_base_url == "https://opencode.ai/zen/v1"
    assert settings.resolved_opencode_plan == "zen"
    assert settings.provider_metadata["base_url_path"] == "/zen/v1"
    metadata = json.dumps(settings.provider_metadata)
    assert secret not in metadata
    assert secret not in repr(settings)


def test_endpoint_normalization_is_narrow_and_anthropic_path_is_preserved(tmp_path: Path) -> None:
    custom = _settings(
        tmp_path,
        anthropic_model="deepseek-v4-flash",
        anthropic_base_url="https://opencode.ai/custom/v1",
        llm_transport="openai-compatible",
    )
    anthropic = _settings(
        tmp_path,
        anthropic_model="claude-test",
        anthropic_base_url="https://anthropic.example/v1",
        llm_provider="anthropic",
    )
    legacy_opencode = _settings(
        tmp_path,
        anthropic_model="deepseek-v4-flash",
        anthropic_base_url="https://opencode.ai/zen/go/v1",
        llm_transport="openai-compatible",
    )
    assert custom.resolved_base_url == "https://opencode.ai/custom/v1"
    assert legacy_opencode.resolved_base_url == "https://opencode.ai/zen/go/v1"
    assert legacy_opencode.resolved_opencode_plan == "go"
    assert anthropic.resolved_transport == "anthropic-compatible"
    assert anthropic.resolved_base_url == "https://anthropic.example/v1"


@pytest.mark.asyncio
async def test_openai_chat_json_path_and_exact_usage_commit(tmp_path: Path) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"tool":"finish"}'))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )
    create = FakeCreate(response=response)
    ledger = _ledger()
    client = LLMClient(
        _settings(
            tmp_path,
            anthropic_model="deepseek-v4-flash",
            anthropic_base_url="https://opencode.ai",
            llm_provider="deepseek",
            opencode_plan="zen",
        ),
        ledger=ledger,
        client=FakeOpenAI(create),
    )

    result = await client.json("system evidence", "user task")

    assert result == {"tool": "finish"}
    call = create.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"] == [
        {"role": "system", "content": "system evidence"},
        {"role": "user", "content": "user task"},
    ]
    assert ledger.snapshot()["used"] == {
        "requests": 1,
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cost_usd": 0.0,
    }
    assert ledger.snapshot()["reserved"]["requests"] == 0
    assert client.usage["input_tokens"] == 11
    assert client.usage["output_tokens"] == 7
    assert client.usage["estimated_tokens"] is False


@pytest.mark.asyncio
async def test_anthropic_messages_path_remains_supported(tmp_path: Path) -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"summary":"ok"}')],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
    )
    create = FakeCreate(response=response)
    client = LLMClient(
        _settings(tmp_path, anthropic_model="claude-test", llm_provider="anthropic"),
        ledger=_ledger(),
        client=FakeAnthropic(create),
    )

    assert await client.json("system", "prompt") == {"summary": "ok"}
    assert create.calls[0]["system"] == "system"
    assert "response_format" not in create.calls[0]
    assert client.transport == "anthropic-compatible"
    assert client.usage["input_tokens"] == 5
    assert client.usage["output_tokens"] == 3


@pytest.mark.asyncio
async def test_provider_exception_cancels_reservation_and_sanitizes_html(tmp_path: Path) -> None:
    class Provider404Error(RuntimeError):
        status_code = 404

    ledger = _ledger()
    create = FakeCreate(error=Provider404Error("<html>" + "not found" * 1000 + "</html>"))
    client = LLMClient(
        _settings(tmp_path, anthropic_model="deepseek-v4-flash", llm_provider="deepseek"),
        ledger=ledger,
        client=FakeOpenAI(create),
    )

    with pytest.raises(LLMTransportError) as captured:
        await client.json("system", "prompt")

    assert captured.value.category == "provider_request_failed"
    assert captured.value.status_code == 404
    assert "<html>" not in str(captured.value)
    assert ledger.snapshot()["used"]["requests"] == 0
    assert ledger.snapshot()["reserved"]["requests"] == 0


@pytest.mark.asyncio
async def test_invalid_json_is_structured_after_usage_is_committed_once(tmp_path: Path) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=4),
    )
    ledger = _ledger()
    client = LLMClient(
        _settings(tmp_path, anthropic_model="deepseek-v4-flash", llm_provider="deepseek"),
        ledger=ledger,
        client=FakeOpenAI(FakeCreate(response=response)),
    )

    with pytest.raises(LLMTransportError) as captured:
        await client.json("system", "prompt")

    assert captured.value.as_dict()["category"] == "invalid_json"
    assert ledger.snapshot()["used"]["requests"] == 1
    assert ledger.snapshot()["used"]["input_tokens"] == 2
    assert ledger.snapshot()["used"]["output_tokens"] == 4
    assert ledger.snapshot()["reserved"]["requests"] == 0


@pytest.mark.asyncio
async def test_openai_token_limit_is_classified_after_exact_usage_commit(tmp_path: Path) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"edits":[{"new_text":"partial source'),
                finish_reason="length",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=9, completion_tokens=32),
    )
    ledger = _ledger()
    client = LLMClient(
        _settings(tmp_path, anthropic_model="deepseek-v4-flash", llm_provider="deepseek"),
        ledger=ledger,
        client=FakeOpenAI(FakeCreate(response=response)),
    )

    with pytest.raises(LLMOutputTruncatedError) as captured:
        await client.json("system", "prompt")

    assert captured.value.category == "provider_output_truncated"
    assert "partial source" not in str(captured.value)
    assert ledger.snapshot()["used"]["requests"] == 1
    assert ledger.snapshot()["used"]["input_tokens"] == 9
    assert ledger.snapshot()["used"]["output_tokens"] == 32
    assert ledger.snapshot()["reserved"]["requests"] == 0


@pytest.mark.asyncio
async def test_anthropic_token_limit_is_classified_after_exact_usage_commit(tmp_path: Path) -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"edits":[{"new_text":"partial source')],
        stop_reason="max_tokens",
        usage=SimpleNamespace(input_tokens=8, output_tokens=32),
    )
    ledger = _ledger()
    client = LLMClient(
        _settings(tmp_path, anthropic_model="claude-test", llm_provider="anthropic"),
        ledger=ledger,
        client=FakeAnthropic(FakeCreate(response=response)),
    )

    with pytest.raises(LLMOutputTruncatedError):
        await client.json("system", "prompt")

    assert ledger.snapshot()["used"]["requests"] == 1
    assert ledger.snapshot()["used"]["input_tokens"] == 8
    assert ledger.snapshot()["used"]["output_tokens"] == 32
    assert ledger.snapshot()["reserved"]["requests"] == 0


@pytest.mark.asyncio
async def test_one_shot_prompt_prefers_compact_replacements_without_tools(tmp_path: Path) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok","edits":[]}'), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2),
    )
    create = FakeCreate(response=response)
    prompt_ledger = BudgetLedger(
        BudgetLimits(
            max_requests=2,
            max_input_tokens=1000,
            max_output_tokens=100,
            max_cost_usd=1,
            reserve_output_tokens=32,
        )
    )
    client = LLMClient(
        _settings(tmp_path, anthropic_model="deepseek-v4-flash", llm_provider="deepseek"),
        ledger=prompt_ledger,
        client=FakeOpenAI(create),
    )

    assert await client.one_shot("goal", "index", "source", "pytest -q") == {"summary": "ok", "edits": []}

    call = create.calls[0]
    prompt = call["messages"][1]["content"]
    system = call["messages"][0]["content"]
    assert "old_text" in prompt
    assert "逐字符复制" in prompt
    assert "不能使用工具或获得迭代反馈" in system
    assert len(create.calls) == 1


@pytest.mark.asyncio
async def test_reasoning_control_forwards_openai_reasoning_effort(tmp_path: Path) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok"}'), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2),
    )

    off_create = FakeCreate(response=response)
    off_client = LLMClient(
        _settings(
            tmp_path,
            anthropic_model="deepseek-v4-flash",
            llm_provider="deepseek",
            llm_reasoning="off",
        ),
        client=FakeOpenAI(off_create),
    )
    await off_client.json("system", "prompt")
    assert off_create.calls[0]["reasoning_effort"] == "none"

    on_create = FakeCreate(response=response)
    on_client = LLMClient(
        _settings(
            tmp_path,
            anthropic_model="deepseek-v4-flash",
            llm_provider="deepseek",
            llm_reasoning="on",
        ),
        client=FakeOpenAI(on_create),
    )
    await on_client.json("system", "prompt")
    assert on_create.calls[0]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_reasoning_auto_leaves_provider_default_untouched(tmp_path: Path) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok"}'), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2),
    )
    create = FakeCreate(response=response)
    client = LLMClient(
        _settings(tmp_path, anthropic_model="deepseek-v4-flash", llm_provider="deepseek"),
        client=FakeOpenAI(create),
    )

    await client.json("system", "prompt")

    assert "reasoning_effort" not in create.calls[0]

    anthropic_response = SimpleNamespace(
        content=[SimpleNamespace(text='{"summary":"ok"}')],
        usage=SimpleNamespace(input_tokens=2, completion_tokens=2),
    )
    anthropic_create = FakeCreate(response=anthropic_response)
    anthropic_client = LLMClient(
        _settings(
            tmp_path,
            anthropic_model="claude-test",
            llm_provider="anthropic",
            llm_reasoning="off",
        ),
        client=FakeAnthropic(anthropic_create),
    )
    await anthropic_client.json("system", "prompt")
    assert "reasoning_effort" not in anthropic_create.calls[0]
