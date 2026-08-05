import asyncio
from pathlib import Path

import pytest

from patchproof.benchmark import BenchmarkHarness
from patchproof.corpus import load_cases
from patchproof.models import BenchmarkCase
from patchproof.policy import ProcessExecutor


def test_all_five_mini_fixtures_fail_before_the_oracle_patch():
    root = Path(__file__).parents[1]
    cases = load_cases(root / "benchmarks" / "manifest.v2.json")
    assert len(cases) == 5

    async def run_initial_checks():
        results = {}
        for case in cases:
            oracle_path, fixed_content = next(iter(case.expected_contents.items()))
            source = root / str(case.local_path) / oracle_path
            assert source.read_text(encoding="utf-8") != fixed_content
            result = await ProcessExecutor(case.resources.output_bytes).run(
                case.required_check_argv,
                cwd=str(root / str(case.local_path)),
                timeout_seconds=case.timeout,
            )
            results[case.id] = result.returncode
        return results

    assert all(returncode != 0 for returncode in asyncio.run(run_initial_checks()).values())


def test_v031_smoke_repairs_each_fixture_with_one_observed_edit():
    root = Path(__file__).parents[1]
    cases = load_cases(root / "benchmarks" / "manifest.v2.json")
    expected_files = {case.id: case.expected_changed_files for case in cases}

    report = asyncio.run(BenchmarkHarness(root).run_deterministic_smoke(cases))

    assert report["execution_policy"] == {
        "initial_failure_required": True,
        "exact_oracle_edits_per_case": 1,
        "oracle_scope": "patchproof_owned_local_fixtures_only",
        "oracle_in_real_path": False,
    }
    assert len(report["initial_checks"]) == 5
    assert all(item["check"]["returncode"] != 0 for item in report["initial_checks"])
    assert len(report["runs"]) == 10
    for run in report["runs"]:
        assert run["success"] is True
        assert run["initial_check_failed"] is True
        assert run["check"]["returncode"] == 0
        assert run["changed_files"] >= 1
        assert run["changed_file_paths"] == expected_files[run["case_id"]]
        assert run["expected_changed_files_verified"] is True
        assert run["patch_size"] > 0
        assert run["oracle_edit_count"] == 1
        if run["variant"] == "harness_tool_loop":
            assert run["tool_calls"] == 3
            assert run["tool_sequence"] == ["apply_edit", "run_check", "finish"]


def test_smoke_rejects_a_fixture_that_already_passes(tmp_path: Path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    fixed = "def value() -> int:\n    return 1\n"
    (fixture / "module.py").write_text(fixed, encoding="utf-8")
    (fixture / "test_module.py").write_text(
        "from module import value\n\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    case = BenchmarkCase.model_validate(
        {
            "id": "already-passing-fixture",
            "suite": "mini-repos",
            "source_kind": "local",
            "local_path": "fixture",
            "issue": "The fixture must begin broken.",
            "goal": "Reject an invalid smoke task.",
            "required_check_argv": ["python", "-m", "pytest", "-q"],
            "image": "local://patchproof-python312",
            "allowed_edit_paths": ["module.py"],
            "expected_changed_files": ["module.py"],
            "expected_contents": {"module.py": fixed},
        }
    )

    with pytest.raises(ValueError, match="already passes"):
        asyncio.run(BenchmarkHarness(tmp_path).run_deterministic_smoke([case]))
