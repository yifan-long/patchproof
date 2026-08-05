from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import patchproof.benchmark as benchmark_module
import patchproof.evaluation as evaluation_module
from patchproof.budget import BudgetExceeded, BudgetLedger, BudgetLimits
from patchproof.config import Settings
from patchproof.corpus import load_cases
from patchproof.evaluation import EvaluationOperationalError, EvaluationOptions, EvaluationOrchestrator
from patchproof.llm import LLMTransportError


def _args(tmp_path: Path) -> argparse.Namespace:
    root = Path(__file__).parents[1]
    return argparse.Namespace(
        command="real",
        manifest=str(root / "benchmarks" / "manifest.v2.json"),
        output=str(tmp_path / "real-failure.json"),
        project_root=str(root),
        confirm_real=True,
        confirm_public_code_egress=False,
        confirm_download=False,
        budget_stage="first-pass",
        max_cases=1,
        repeats=2,
        max_requests=4,
        max_tokens=100,
        max_cost_usd=2.0,
        jsonl=str(tmp_path / "runs.jsonl"),
    )


def _settings(tmp_path: Path, secret: str) -> Settings:
    return Settings(
        env_file_path=str(tmp_path / "missing.env"),
        anthropic_api_key=secret,
        anthropic_model="deepseek-v4-flash",
        anthropic_base_url="https://opencode.ai",
        llm_provider="deepseek",
    )


def test_real_cli_401_writes_redacted_atomic_failure_without_traceback(tmp_path: Path, monkeypatch, capsys) -> None:
    secret = "super-secret-provider-key"
    unsafe_body = "<html>Insufficient balance https://billing.example/private workspace-source-text</html>"
    settings = _settings(tmp_path, secret)

    class ProviderFailureOrchestrator:
        def __init__(self, project_root):
            self.project_root = project_root

        async def run(self, *args, **kwargs):
            raise LLMTransportError("provider_request_failed", unsafe_body, status_code=401)

    monkeypatch.setattr(benchmark_module, "Settings", lambda: settings)
    monkeypatch.setattr(evaluation_module, "EvaluationOrchestrator", ProviderFailureOrchestrator)
    args = _args(tmp_path)

    with pytest.raises(SystemExit) as captured:
        benchmark_module._run_cli(args)

    assert captured.value.code == 2
    output = Path(args.output)
    assert output.is_file()
    report = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["schema_version"] == "patchproof.real-evaluation-failure.v1"
    assert report["patchproof_version"] == "0.3.7"
    assert report["success"] is False
    assert report["failure_category"] == "provider_auth_or_credits"
    assert report["comparison"]["head_to_head_eligible"] is False
    assert report["comparison"]["status"] == "not_produced"
    assert report["selection"]["case_ids"] == ["mini-validation"]
    assert report["selection"]["repeats"] == 2
    assert report["budget"]["ledger"]["used"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }
    assert report["budget"]["ledger"]["reserved"]["requests"] == 0
    assert "runs" not in report and "results" not in report
    assert secret not in serialized
    assert "<html>" not in serialized
    assert "billing.example" not in serialized
    assert "workspace-source-text" not in serialized
    captured_streams = capsys.readouterr()
    assert "Traceback" not in captured_streams.out
    assert "Traceback" not in captured_streams.err
    assert not list(tmp_path.glob("real-failure.json.*.tmp"))


def test_real_cli_budget_failure_is_nonzero_without_fake_results(tmp_path: Path, monkeypatch, capsys) -> None:
    settings = _settings(tmp_path, "test-budget-key")
    ledger = BudgetLedger(
        BudgetLimits(
            max_requests=4,
            max_input_tokens=100,
            max_output_tokens=100,
            max_cost_usd=2,
            reserve_output_tokens=32,
        )
    )

    class BudgetFailureOrchestrator:
        def __init__(self, project_root):
            self.project_root = project_root

        async def run(self, *args, **kwargs):
            raise BudgetExceeded("max_cost_usd", ledger.snapshot())

    monkeypatch.setattr(benchmark_module, "Settings", lambda: settings)
    monkeypatch.setattr(evaluation_module, "EvaluationOrchestrator", BudgetFailureOrchestrator)
    args = _args(tmp_path)

    with pytest.raises(SystemExit) as captured:
        benchmark_module._run_cli(args)

    assert captured.value.code == 2
    report = json.loads(Path(args.output).read_text(encoding="utf-8"))
    assert report["success"] is False
    assert report["failure_category"] == "budget_exhausted"
    assert report["budget"]["stage"] == "first-pass"
    assert report["budget"]["limits"]["max_cost_usd"] == 2.0
    assert report["budget"]["ledger"]["used"]["requests"] == 0
    assert report["budget"]["ledger"]["reserved"]["requests"] == 0
    assert report["comparison"]["complete_pairs_before_failure"] == 0
    assert "runs" not in report and "results" not in report
    streams = capsys.readouterr()
    assert "Traceback" not in streams.out + streams.err


def test_real_cli_truncation_artifact_is_redacted_and_has_no_fake_results(tmp_path: Path, monkeypatch) -> None:
    secret_source = "PRIVATE_SOURCE_TEXT_SHOULD_NOT_LEAK"
    settings = _settings(tmp_path, "private-provider-key")

    class TruncatedOrchestrator:
        def __init__(self, project_root):
            self.project_root = project_root

        async def run(self, *args, **kwargs):
            raise LLMTransportError("provider_output_truncated", secret_source)

    monkeypatch.setattr(benchmark_module, "Settings", lambda: settings)
    monkeypatch.setattr(evaluation_module, "EvaluationOrchestrator", TruncatedOrchestrator)
    args = _args(tmp_path)

    with pytest.raises(SystemExit) as captured:
        benchmark_module._run_cli(args)

    assert captured.value.code == 2
    report = json.loads(Path(args.output).read_text(encoding="utf-8"))
    serialized = json.dumps(report)
    assert report["failure_category"] == "provider_output_truncated"
    assert report["message"] == "provider output reached the configured token limit"
    assert report["comparison"]["head_to_head_eligible"] is False
    assert "runs" not in report and "results" not in report
    assert secret_source not in serialized


@pytest.mark.asyncio
async def test_provider_failure_inside_pair_writes_no_standalone_variant(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    case = load_cases(root / "benchmarks" / "manifest.v2.json")[0]
    jsonl = tmp_path / "partial.jsonl"

    class CancelledProviderModel:
        def __init__(self):
            self.ledger = None
            self.metadata = {"provider": "fake-provider-failure"}
            self.usage = {}

        async def one_shot(self, *args, **kwargs):
            reservation = self.ledger.reserve(input_tokens=2, requested_output_tokens=4)
            self.ledger.cancel(reservation)
            raise LLMTransportError("provider_request_failed", "unsafe response body", status_code=401)

    with pytest.raises(EvaluationOperationalError) as captured:
        await EvaluationOrchestrator(root).run(
            [case],
            settings=_settings(tmp_path, "pair-test-key"),
            options=EvaluationOptions(repeats=1, max_requests=4, max_tokens=100, max_cost_usd=2),
            model_factory=lambda variant, selected_case: CancelledProviderModel(),
            jsonl_path=jsonl,
        )

    assert captured.value.failure_category == "provider_auth_or_credits"
    assert captured.value.complete_pairs == 0
    assert captured.value.partial_records == 0
    assert captured.value.ledger["used"]["requests"] == 0
    assert captured.value.ledger["reserved"]["requests"] == 0
    assert not jsonl.exists()
