from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from patchproof.config import Settings
from patchproof.evaluation import EvaluationOptions, EvaluationOrchestrator
from patchproof.models import BenchmarkCase
from patchproof.repo_index import RepoIndex
from patchproof.workspace import SnapshotWorkspace, WorkspaceBoundaryError


class GateExecutor:
    def __init__(self, returncodes: list[int], events: list[str], canary: str):
        self.returncodes = list(returncodes)
        self.events = events
        self.canary = canary
        self.calls = 0

    async def run(self, spec, *, cwd, timeout_seconds, cancel_event=None):
        self.calls += 1
        self.events.append(f"check:{self.calls}")
        assert not (Path(cwd) / "bug_patch.txt").exists()
        returncode = self.returncodes.pop(0) if self.returncodes else 1
        stdout = "tests/test_target.py:7: AssertionError: expected 2, got 1" if returncode else "OK"
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr="",
            timed_out=False,
            cancelled=False,
            output_truncated=False,
            as_dict=lambda: {
                "returncode": returncode,
                "stdout": stdout,
                "stderr": "",
                "timed_out": False,
                "cancelled": False,
                "output_truncated": False,
            },
        )


class CaptureBaseline:
    def __init__(self):
        self.metadata = {"provider": "offline"}
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        self.first_context: tuple[str, str, str, str] | None = None
        self.ledger = None

    async def one_shot(self, goal, index_context, source_context, check_command):
        self.usage["requests"] += 1
        self.first_context = goal, index_context, source_context, check_command
        return {"summary": "no repair", "edits": []}


class CaptureHarness:
    def __init__(self):
        self.metadata = {"provider": "offline"}
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        self.first_context: tuple[str, str, str, str] | None = None
        self.ledger = None

    async def plan(self, goal, index_context, source_context, check_command):
        self.usage["requests"] += 1
        self.first_context = goal, index_context, source_context, check_command
        return {"summary": "observed initial failure", "steps": [], "checks": []}

    async def next_action(self, goal, plan, context, observations, step):
        self.usage["requests"] += 1
        return {"tool": "finish", "summary": "no edit", "verdict": "blocked"}


def _case() -> BenchmarkCase:
    return BenchmarkCase.model_validate(
        {
            "id": "bugsinpy-demo-1",
            "suite": "public-bugsinpy",
            "source_kind": "bugsinpy",
            "repo_url": "https://example.test/project",
            "project": "demo",
            "bug_id": 1,
            "immutable_revision": "a" * 40,
            "license_spdx": "MIT",
            "source_url": "https://example.test/BugsInPy/projects/demo/bugs/1",
            "issue": "Official demo bug 1",
            "goal": "Repair BugsInPy demo bug 1; official failing test: tests.test_target.TestBug.test_value.",
            "python_version": "3.12.0",
            "test_file": "tests/test_target.py",
            "executable_state": "verified_failing",
            "required_check_argv": ["python", "-m", "unittest", "-q", "tests.test_target.TestBug.test_value"],
            "image": "sha256:" + "b" * 64,
            "privacy_public_code": True,
            "provenance_state": "resolved",
        }
    )


def _repo(tmp_path: Path, canary: str) -> Path:
    repo = tmp_path / "source"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_target.py").write_text(
        "from target import value\n\nclass TestBug:\n    def test_value(self):\n        assert value() == 2\n",
        encoding="utf-8",
    )
    (repo / "target.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "irrelevant.py").write_text("UNRELATED_CANARY = 99\n", encoding="utf-8")
    (repo / "bug_patch.txt").write_text(canary, encoding="utf-8")
    return repo


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env_file_path=str(tmp_path / "missing.env"),
        profile_file_path=str(tmp_path / "missing-profile.env"),
        anthropic_api_key="",
        max_tool_steps=1,
        max_iterations=1,
    )


def test_oracle_artifact_is_denied_from_index_reads_and_diff_reports(tmp_path: Path) -> None:
    source = _repo(tmp_path, "RUNTIME_ORACLE_CANARY")
    workspace = SnapshotWorkspace(source, tmp_path / "staging")
    workspace.create()

    index = RepoIndex.build(workspace.staging)
    assert "bug_patch.txt" not in index.files
    assert "RUNTIME_ORACLE_CANARY" not in index.source_context("bug patch")
    with pytest.raises(WorkspaceBoundaryError, match="oracle"):
        workspace.read_file("bug_patch.txt")
    diff, changed = workspace.diff()
    assert "bug_patch.txt" not in changed
    assert "RUNTIME_ORACLE_CANARY" not in diff


@pytest.mark.asyncio
async def test_gate_rejects_already_passing_before_model_factory(tmp_path: Path) -> None:
    events: list[str] = []
    executor = GateExecutor([0, 0], events, "ORACLE_CANARY")
    orchestrator = EvaluationOrchestrator(tmp_path, initial_check_executor=executor)
    factory_calls: list[str] = []

    result = await orchestrator._run_pair(
        _case(),
        _repo(tmp_path, "ORACLE_CANARY"),
        1,
        "pair-1",
        _settings(tmp_path),
        lambda variant, case: factory_calls.append(variant),
        ledger=SimpleNamespace(),
    )

    assert factory_calls == []
    assert events == ["check:1", "check:2"]
    assert {item["failure_category"] for item in result} == {"initial_check_already_passes"}
    assert all(item["status"] == "invalid" and item["usage"] == {} for item in result)


@pytest.mark.asyncio
async def test_equal_initial_failure_evidence_focuses_context_and_excludes_oracle(tmp_path: Path) -> None:
    canary = "PATCHPROOF_PRIVATE_PATCH_CANARY"
    events: list[str] = []
    executor = GateExecutor([1, 1, 1], events, canary)
    baseline = CaptureBaseline()
    harness = CaptureHarness()

    def factory(variant, case):
        events.append(f"model:{variant}")
        return baseline if variant == "baseline" else harness

    records = await EvaluationOrchestrator(tmp_path, initial_check_executor=executor)._run_pair(
        _case(),
        _repo(tmp_path, canary),
        1,
        "pair-1",
        _settings(tmp_path),
        factory,
        ledger=SimpleNamespace(),
    )

    assert events[:4] == ["check:1", "check:2", "model:baseline", "model:harness"]
    assert baseline.first_context is not None and harness.first_context is not None
    assert baseline.first_context == harness.first_context
    _, index_context, source_context, check_command = baseline.first_context
    assert "AssertionError: expected 2, got 1" in source_context
    assert "--- tests/test_target.py ---" in source_context
    assert "UNRELATED_CANARY" not in source_context
    assert canary not in source_context
    assert "bug_patch.txt" not in index_context + source_context
    assert check_command == "python -m unittest -q tests.test_target.TestBug.test_value"
    baseline_evidence = records[0]["initial_failure_evidence"]
    harness_evidence = records[1]["initial_failure_evidence"]
    assert baseline_evidence == harness_evidence
    assert len(baseline_evidence["snapshot_sha256"]) == 64
    assert len(baseline_evidence["evidence_sha256"]) == 64
    serialized = json.dumps(records)
    assert canary not in serialized
    assert "bug_patch.txt" not in serialized


@pytest.mark.asyncio
async def test_unverified_public_runtime_is_blocked_before_source_or_model(tmp_path: Path) -> None:
    case = _case().model_copy(update={"executable_state": "environment_unreproducible"})
    factory_calls: list[str] = []
    report = await EvaluationOrchestrator(tmp_path).run(
        [case],
        settings=_settings(tmp_path),
        options=EvaluationOptions(
            confirm_real=True,
            confirm_download=True,
            confirm_public_code_egress=True,
        ),
        model_factory=lambda variant, selected: factory_calls.append(variant),
    )

    assert report["success"] is False
    assert report["budget"]["stop_reason"] == "public_executable_failure_unverified"
    assert factory_calls == []
