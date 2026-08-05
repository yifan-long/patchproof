from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from patchproof.benchmark import BenchmarkHarness
from patchproof.config import Settings
from patchproof.corpus import load_cases


class OneShotFake:
    def __init__(self, proposal: dict):
        self.proposal = proposal
        self.calls = 0
        self.metadata = {"provider": "offline-fake"}
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}

    async def one_shot(self, *args, **kwargs):
        self.calls += 1
        self.usage["requests"] += 1
        return self.proposal


class PassingExecutor:
    async def run(self, spec, cwd, timeout_seconds):
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            cancelled=False,
            output_truncated=False,
            as_dict=lambda: {
                "argv": spec.argv,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "cancelled": False,
                "output_truncated": False,
            }
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env_file_path=str(tmp_path / "missing.env"),
        profile_file_path=str(tmp_path / "missing-profile.env"),
        anthropic_api_key="",
    )


def _case(tmp_path: Path):
    root = Path(__file__).parents[1]
    source = load_cases(root / "benchmarks" / "manifest.v2.json")[0]
    return source.model_copy(
        update={
            "id": "compact-one-shot-test",
            "goal": "replace the faulty value",
            "required_check_argv": ["python", "-m", "compileall", "-q", "module.py"],
            "allowed_edit_paths": ["module.py"],
            "expected_changed_files": ["module.py"],
        }
    )


async def _run(tmp_path: Path, proposal: dict) -> tuple[dict, OneShotFake, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_bytes(b"VALUE = 1\nKEEP = 2\n")
    model = OneShotFake(proposal)
    result = await BenchmarkHarness(tmp_path)._run_real_baseline(
        _case(tmp_path),
        repo,
        model,
        _settings(tmp_path),
        executor=PassingExecutor(),
    )
    return result, model, repo.parent / "staging" / "module.py"


@pytest.mark.asyncio
async def test_compact_edit_replaces_unique_text_without_copying_unchanged_source(tmp_path: Path) -> None:
    result, model, staged = await _run(
        tmp_path,
        {
            "summary": "small replacement",
            "edits": [{"path": "module.py", "old_text": "VALUE = 1", "new_text": "VALUE = 3"}],
        },
    )

    assert result["success"] is True, json.dumps(result, indent=2)
    assert result["changed_files"] == 1
    assert result["changed_file_paths"] == ["module.py"]
    assert result["patch_size"] > 0
    assert staged.read_text(encoding="utf-8") == "VALUE = 3\nKEEP = 2\n"
    assert model.calls == 1
    assert result["one_shot_request_count"] == 1
    assert result["tool_calls"] == 0
    assert result["edit_evidence"] == [
        {"path": "module.py", "mode": "compact_replacement", "precondition": "unique_old_text"}
    ]
    evidence = json.dumps(result["edit_evidence"])
    assert "VALUE = 1" not in evidence
    assert "KEEP = 2" not in evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit",
    [
        {"path": "module.py", "old_text": "=", "new_text": "=="},
        {"path": "module.py", "old_text": "VALUE = 999", "new_text": "VALUE = 3"},
        {"path": "module.py", "old_text": "VALUE = 1", "new_text": "VALUE = 3", "expected_sha256": "0" * 64},
    ],
    ids=["ambiguous", "missing", "stale-hash"],
)
async def test_compact_edit_rejects_ambiguous_missing_and_stale_preconditions(tmp_path: Path, edit: dict) -> None:
    result, model, staged = await _run(tmp_path, {"summary": "reject me", "edits": [edit]})

    assert result["success"] is False
    assert result["failure_category"] == "baseline_precondition_failed"
    assert result["changed_files"] == 0
    assert result["changed_file_paths"] == []
    assert staged.read_text(encoding="utf-8") == "VALUE = 1\nKEEP = 2\n"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_full_file_one_shot_remains_backward_compatible(tmp_path: Path) -> None:
    replacement = "VALUE = 3\nKEEP = 2\n"
    result, model, staged = await _run(
        tmp_path,
        {"summary": "legacy full file", "edits": [{"path": "module.py", "new_text": replacement}]},
    )

    assert result["success"] is True, json.dumps(result, indent=2)
    assert staged.read_text(encoding="utf-8") == replacement
    assert model.calls == 1
    assert result["edit_evidence"] == [
        {"path": "module.py", "mode": "full_file", "precondition": "snapshot_sha256"}
    ]


@pytest.mark.asyncio
async def test_compact_edit_accepts_matching_current_hash(tmp_path: Path) -> None:
    current = "VALUE = 1\nKEEP = 2\n"
    result, _, _ = await _run(
        tmp_path,
        {
            "edits": [
                {
                    "path": "module.py",
                    "old_text": "VALUE = 1",
                    "new_text": "VALUE = 3",
                    "expected_sha256": hashlib.sha256(current.encode()).hexdigest(),
                }
            ]
        },
    )

    assert result["success"] is True
    assert result["edit_evidence"][0]["precondition"] == "unique_old_text+expected_sha256"
