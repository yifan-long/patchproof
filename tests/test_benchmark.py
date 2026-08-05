import asyncio
import hashlib
import json
from pathlib import Path

from patchproof.benchmark import BenchmarkHarness, aggregate_metrics, load_cases
from patchproof.llm import FakeLLM


def test_benchmark_aggregation_keeps_missing_values_missing():
    result = aggregate_metrics(
        [
            {"variant": "baseline", "success": True, "steps": 1},
            {"variant": "baseline", "success": False, "steps": 2},
            {"variant": "harness", "success": True, "steps": 4, "tool_calls": 4},
        ]
    )
    assert result["variants"]["baseline"]["success_rate"] == 0.5
    assert result["variants"]["harness"]["mean_tool_calls"] == 4
    assert "mean_tool_calls" not in result["variants"]["baseline"]


def test_deterministic_smoke_produces_baseline_and_harness(tmp_path: Path):
    root = Path(__file__).parents[1]
    cases = load_cases(root / "benchmarks" / "cases" / "smoke.json")
    report = asyncio.run(BenchmarkHarness(root).run_deterministic_smoke(cases))
    assert {item["variant"] for item in report["runs"]} == {"baseline_one_shot", "harness_tool_loop"}
    harness = next(item for item in report["runs"] if item["variant"] == "harness_tool_loop")
    assert harness["success"] is True
    output = tmp_path / "report.json"
    output.write_text(json.dumps(report), encoding="utf-8")
    assert output.is_file()


def test_real_benchmark_invokes_one_shot_and_harness_with_comparable_metrics(tmp_path: Path):
    root = Path(__file__).parents[1]
    cases = load_cases(root / "benchmarks" / "cases" / "smoke.json")
    case = cases[0]
    expected = case.expected_contents["app.py"]
    source_hash = hashlib.sha256(
        (root / case.fixture / "app.py").read_bytes()
    ).hexdigest()
    calls = {"baseline_one_shot_real": 0, "harness_tool_loop_real": 0}
    oracle_views = []

    def factory(variant, selected_case):
        calls[variant] += 1
        oracle_views.append(selected_case.expected_contents)
        if variant == "baseline_one_shot_real":
            return FakeLLM(
                [],
                one_shot_result={"summary": "fake one-shot", "edits": [{"path": "app.py", "new_text": expected}]},
            )
        return FakeLLM(
            [
                {
                    "tool": "apply_edit",
                    "path": "app.py",
                    "new_text": expected,
                    "expected_sha256": source_hash,
                },
                {"tool": "run_check", "argv": ["python", "-m", "pytest", "-q"]},
                {"tool": "finish", "summary": "fake harness", "verdict": "verified"},
            ]
        )

    report = asyncio.run(
        BenchmarkHarness(root).run_real(
            cases,
            max_cases=1,
            max_cost_usd=10,
            model_factory=factory,
        )
    )
    assert calls == {"baseline_one_shot_real": 1, "harness_tool_loop_real": 1}
    assert {run["variant"] for run in report["runs"]} == {
        "baseline_one_shot_real",
        "harness_tool_loop_real",
    }
    assert all(run["check_command"] == case.check_command for run in report["runs"])
    assert all("success" in run and "duration_ms" in run and "usage" in run for run in report["runs"])
    harness = next(run for run in report["runs"] if run["variant"] == "harness_tool_loop_real")
    assert harness["required_check_verified"] is True
    assert harness["receipt_file_verified"] is True
    assert report["evaluation_kind"] == "model_quality_comparison"
    assert report["execution_policy"]["oracle_in_real_path"] is False
    assert oracle_views == [{}, {}]
