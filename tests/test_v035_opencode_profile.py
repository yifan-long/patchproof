from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchproof.config import ProviderConfigurationError, Settings


def _deepseek_settings(tmp_path: Path, **values) -> Settings:
    values.setdefault("anthropic_api_key", "test-only-key")
    values.setdefault("anthropic_model", "deepseek-v4-flash")
    values.setdefault("anthropic_base_url", "https://opencode.ai")
    values.setdefault("llm_provider", "deepseek")
    return Settings(
        env_file_path=str(tmp_path / "missing-provider.env"),
        profile_file_path=str(tmp_path / "missing-profile.env"),
        **values,
    )


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("go", "https://opencode.ai/zen/go/v1"),
        ("zen", "https://opencode.ai/zen/v1"),
    ],
)
def test_root_opencode_url_resolves_by_explicit_profile(tmp_path: Path, profile: str, expected: str) -> None:
    settings = _deepseek_settings(tmp_path, opencode_plan=profile)
    assert settings.resolved_base_url == expected
    assert settings.resolved_opencode_plan == profile
    assert settings.provider_metadata["profile"] == profile
    assert settings.provider_metadata["transport"] == "openai-compatible"
    assert settings.provider_metadata["base_url_host"] == "opencode.ai"
    assert settings.provider_metadata["base_url_path"] == expected.removeprefix("https://opencode.ai")


def test_root_opencode_url_with_auto_profile_fails_closed(tmp_path: Path) -> None:
    settings = _deepseek_settings(tmp_path, opencode_plan="auto")
    with pytest.raises(ProviderConfigurationError, match="ambiguous"):
        _ = settings.resolved_base_url
    with pytest.raises(ProviderConfigurationError, match="PATCHPROOF_OPENCODE_PLAN"):
        _ = settings.provider_metadata


@pytest.mark.parametrize(
    ("url", "profile"),
    [
        ("https://opencode.ai/zen/go/v1", "go"),
        ("https://opencode.ai/zen/v1", "zen"),
    ],
)
def test_explicit_official_paths_are_preserved_and_authoritative(
    tmp_path: Path, url: str, profile: str
) -> None:
    settings = _deepseek_settings(tmp_path, anthropic_base_url=url, opencode_plan="auto")
    assert settings.resolved_base_url == url
    assert settings.resolved_opencode_plan == profile


def test_custom_https_host_and_path_are_not_rewritten(tmp_path: Path) -> None:
    url = "https://gateway.example/custom/openai/v1"
    settings = _deepseek_settings(tmp_path, anthropic_base_url=url, opencode_plan="go")
    assert settings.resolved_base_url == url
    assert settings.provider_metadata["base_url_host"] == "gateway.example"
    assert settings.provider_metadata["base_url_path"] == "/custom/openai/v1"


def test_profile_precedence_process_then_local_then_constructor_then_archive(tmp_path: Path, monkeypatch) -> None:
    provider = tmp_path / "archive.env"
    provider.write_text(
        "DEEPSEEK_API_KEY=archive-only-secret\n"
        "DEEPSEEK_BASE_URL=https://opencode.ai\n"
        "DEEPSEEK_MODEL=deepseek-v4-flash\n"
        "OPENCODE_PLAN=zen\n",
        encoding="utf-8",
    )
    local = tmp_path / "local.env"
    local.write_text(
        "PATCHPROOF_OPENCODE_PLAN=go\n"
        "PATCHPROOF_ANTHROPIC_API_KEY=must-not-load-local-secret\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PATCHPROOF_OPENCODE_PLAN", raising=False)
    monkeypatch.delenv("PATCHPROOF_ANTHROPIC_API_KEY", raising=False)

    local_wins = Settings(_env_file=str(provider), profile_file_path=str(local), opencode_plan="zen")
    assert local_wins.opencode_plan == "go"
    assert local_wins.anthropic_api_key == "archive-only-secret"
    assert "must-not-load-local-secret" not in repr(local_wins)
    assert "archive-only-secret" not in json.dumps(local_wins.provider_metadata)

    monkeypatch.setenv("PATCHPROOF_OPENCODE_PLAN", "zen")
    process_wins = Settings(_env_file=str(provider), profile_file_path=str(local), opencode_plan="go")
    assert process_wins.opencode_plan == "zen"

    monkeypatch.delenv("PATCHPROOF_OPENCODE_PLAN", raising=False)
    constructor_wins = Settings(
        _env_file=str(provider),
        profile_file_path=str(tmp_path / "missing-local.env"),
        opencode_plan="go",
    )
    assert constructor_wins.opencode_plan == "go"

    archive_wins = Settings(
        _env_file=str(provider),
        profile_file_path=str(tmp_path / "missing-local.env"),
    )
    assert archive_wins.opencode_plan == "zen"


def test_persisted_local_profile_contains_only_nonsecret_go_selection() -> None:
    root = Path(__file__).parents[1]
    profile = root / ".patchproof.local.env"
    text = profile.read_text(encoding="utf-8")
    assignments = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert assignments == ["PATCHPROOF_OPENCODE_PLAN=go"]
    assert "API_KEY" not in text
    assert ".patchproof.local.env" in (root / ".gitignore").read_text(encoding="utf-8")
