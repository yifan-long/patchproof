import asyncio
import hashlib
from pathlib import Path

from patchproof.budget import BudgetExceeded, BudgetLedger, BudgetLimits
from patchproof.evaluation import EvaluationOptions, EvaluationOrchestrator, canonical_aggregate
from patchproof.faults import FaultRunner
from patchproof.llm import FakeLLM


def test_fault_runner_executes_all_twelve_hooks_offline():
    report = FaultRunner().run_all()
    assert report["offline"] is True
    assert report["scenario_count"] == 12
    assert report["passed"] is True
    assert len(report["results"]) == 12


def test_shared_budget_reserves_worst_case_before_second_request():
    ledger = BudgetLedger(
        BudgetLimits(max_requests=2, max_input_tokens=10, max_output_tokens=4, max_cost_usd=1, reserve_output_tokens=4)
    )
    first = ledger.reserve(input_tokens=1, requested_output_tokens=4)
    try:
        ledger.reserve(input_tokens=1, requested_output_tokens=4)
    except BudgetExceeded as exc:
        assert exc.reason == "max_output_tokens"
    else:
        raise AssertionError("second worst-case reservation should be rejected")
    ledger.cancel(first)


def test_partial_pairs_are_retained_but_excluded_from_head_to_head():
    aggregate = canonical_aggregate(
        [
            {"case_id": "a", "pair_id": "a:1", "variant": "baseline", "status": "completed", "success": True},
            {"case_id": "a", "pair_id": "a:1", "variant": "harness", "status": "completed", "success": False},
            {"case_id": "b", "pair_id": "b:1", "variant": "baseline", "status": "completed", "success": True},
        ]
    )
    assert aggregate["runs"] == 3
    assert aggregate["complete_pairs"] == 1
    assert aggregate["partial_runs"] == 1
    assert aggregate["head_to_head"]["runs"] == 2


def test_fake_evaluation_repeats_fresh_pairs_and_labels_local_smoke():
    root = Path(__file__).parents[1]
    from patchproof.corpus import load_cases

    case = load_cases(root / "benchmarks" / "manifest.v2.json")[0]
    oracle_path, fixed_content = next(iter(case.expected_contents.items()))
    source_hash = hashlib.sha256((root / case.local_path / oracle_path).read_bytes()).hexdigest()
    factory_oracle_views = []

    def factory(variant, selected):
        factory_oracle_views.append(selected.expected_contents)
        if variant == "baseline":
            return FakeLLM(
                [],
                one_shot_result={
                    "summary": "repair",
                    "edits": [{"path": oracle_path, "new_text": fixed_content}],
                },
            )
        return FakeLLM(
            [
                {
                    "tool": "apply_edit",
                    "path": oracle_path,
                    "new_text": fixed_content,
                    "expected_sha256": source_hash,
                },
                {"tool": "run_check", "argv": ["python", "-m", "pytest", "-q"]},
                {"tool": "finish", "summary": "verified", "verdict": "verified"},
            ]
        )

    report = asyncio.run(
        EvaluationOrchestrator(root).run(
            [case],
            options=EvaluationOptions(repeats=2, max_requests=20, max_tokens=10000, max_cost_usd=2),
            model_factory=factory,
        )
    )
    assert len(report["runs"]) == 4
    assert report["execution_policy"]["fresh_isolated_copies"] is True
    assert report["execution_policy"]["oracle_in_real_path"] is False
    assert report["aggregate"]["complete_pairs"] == 2
    assert all(item["execution_mode"] == "local_smoke_only" for item in report["runs"])
    assert all(view == {} for view in factory_oracle_views)
