import json
from pathlib import Path

import pytest

from patchproof.config import Settings
from patchproof.corpus import build_fetch_plan, content_addressed_cache_key, execute_fetch_plan, load_cases
from patchproof.models import BenchmarkCase


def test_deepseek_env_mapping_and_patchproof_precedence_without_secret_metadata(tmp_path: Path, monkeypatch):
    secret = "do-not-print-this-key"
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        f"DEEPSEEK_API_KEY={secret}\nDEEPSEEK_BASE_URL=https://provider.example/anthropic\n"
        "DEEPSEEK_MODEL=file-model\nDEEPSEEK_MAX_TOKENS=777\nMODEL_COST_PER_MILLION_TOKENS=1.25\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PATCHPROOF_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATCHPROOF_ANTHROPIC_MODEL", "override-model")
    settings = Settings(_env_file=str(env_file))
    assert settings.anthropic_api_key == secret
    assert settings.anthropic_model == "override-model"
    assert settings.anthropic_base_url == "https://provider.example/anthropic"
    assert settings.anthropic_max_tokens == 777
    assert settings.model_cost_per_million_tokens == 1.25
    assert settings.resolved_transport == "openai-compatible"
    assert settings.resolved_provider == "deepseek"
    metadata = json.dumps(settings.provider_metadata, ensure_ascii=False)
    assert secret not in metadata
    assert secret not in repr(settings)


def test_v02_corpus_has_five_mini_repos_and_seven_unresolved_official_descriptors():
    root = Path(__file__).parents[1]
    mini = load_cases(root / "benchmarks" / "manifest.v2.json")
    public = load_cases(root / "benchmarks" / "public" / "bugs-in-py.v2.json")
    assert len(mini) == 5
    assert len(public) == 7
    assert all(case.source_kind == "local" for case in mini)
    assert all(len(case.expected_contents) == 1 for case in mini)
    assert all(case.provenance_state == "unresolved" for case in public)
    assert {case.project for case in public} >= {"youtube-dl", "PySnooper", "black", "cookiecutter", "fastapi"}
    assert all(case.immutable_revision is None and case.license_spdx is None and case.image is None for case in public)
    assert all(case.expected_contents == {} for case in public)


def test_public_case_rejects_local_fixture_oracle():
    root = Path(__file__).parents[1]
    public = load_cases(root / "benchmarks" / "public" / "bugs-in-py.v2.json")[0]
    payload = public.model_dump(mode="json")
    payload["allowed_edit_paths"] = ["module.py"]
    payload["expected_changed_files"] = ["module.py"]
    payload["expected_contents"] = {"module.py": "fixed = True\n"}
    with pytest.raises(ValueError, match="restricted"):
        BenchmarkCase.model_validate(payload)


def test_public_resolved_case_rejects_floating_revision_and_unknown_license():
    base = {
        "id": "public-resolved",
        "suite": "public",
        "source_kind": "bugsinpy",
        "repo_url": "https://example.test/repo",
        "project": "example",
        "bug_id": 1,
        "source_url": "https://example.test/metadata",
        "issue": "issue",
        "goal": "goal",
        "required_check_argv": ["python", "-m", "pytest", "-q"],
        "privacy_public_code": True,
        "provenance_state": "resolved",
        "license_spdx": "unknown",
        "immutable_revision": "main",
        "image": "python:3.12",
    }
    with pytest.raises(ValueError):
        BenchmarkCase.model_validate(base)


def test_case_v2_timeout_shell_and_image_contracts():
    local = load_cases(Path(__file__).parents[1] / "benchmarks" / "manifest.v2.json")[0]
    payload = local.model_dump(mode="json")
    payload["timeout"] = 13
    payload.pop("timeout_seconds", None)
    parsed = BenchmarkCase.model_validate(payload)
    assert parsed.timeout == 13
    assert parsed.timeout_seconds == 13
    assert "timeout" in parsed.model_dump(mode="json")

    shell_payload = {**payload, "required_check_argv": ["sh", "-c", "pytest -q"]}
    with pytest.raises(ValueError, match="invoke a shell"):
        BenchmarkCase.model_validate(shell_payload)

    floating_digest = {**payload, "image": "python:latest@sha256:" + "a" * 64}
    with pytest.raises(ValueError, match="latest"):
        BenchmarkCase.model_validate(floating_digest)


def test_fetch_plan_is_content_addressed_and_never_downloads_without_confirmation(tmp_path: Path):
    case = BenchmarkCase.model_validate(
        {
            "id": "git-pinned-case",
            "suite": "public",
            "source_kind": "git",
            "repo_url": "https://example.test/repo.git",
            "immutable_revision": "a" * 40,
            "license_spdx": "MIT",
            "source_url": "https://example.test/source",
            "issue": "issue",
            "goal": "goal",
            "required_check_argv": ["python", "-m", "pytest", "-q"],
            "image": "python:3.12@sha256:" + "b" * 64,
        }
    )
    plan = build_fetch_plan(case, tmp_path / "cache")
    assert plan.status == "confirmation_required"
    assert plan.confirmation_required is True
    assert all("git" == command[0] for command in plan.commands)
    assert content_addressed_cache_key(case) == plan.cache_key

    class FakeRunner:
        def __init__(self):
            self.calls = []

        def run(self, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return {
                "returncode": 0,
                "stdout": "a" * 40 if argv[1:3] == ["-C", str(plan.cache_path)] else "",
                "stderr": "",
            }

    runner = FakeRunner()
    blocked = execute_fetch_plan(plan, runner=runner)
    assert blocked["status"] == "confirmation_required"
    assert runner.calls == []
