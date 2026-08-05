import asyncio
from pathlib import Path

import pytest

from patchproof.agent_tools import InvalidToolAction, parse_tool_action
from patchproof.models import ApplyEditAction
from patchproof.policy import ProcessExecutor, classify_argv, classify_command
from patchproof.receipt import build_patch_receipt, verify_receipt, verify_receipt_file, write_receipt_atomic
from patchproof.workspace import SnapshotWorkspace, WorkspacePreconditionError


def test_typed_action_rejects_unknown_tools_and_extra_fields():
    with pytest.raises(InvalidToolAction):
        parse_tool_action({"tool": "run_python", "code": "print(1)"})
    with pytest.raises(InvalidToolAction):
        parse_tool_action({"tool": "inspect_diff", "extra": True})
    action = parse_tool_action(
        {
            "tool": "apply_edit",
            "path": "app.py",
            "new_text": "x\n",
            "expected_sha256": "0" * 64,
        }
    )
    assert isinstance(action, ApplyEditAction)


def test_apply_edit_requires_fresh_precondition(tmp_path: Path):
    original = tmp_path / "repo"
    original.mkdir()
    source = original / "app.py"
    source.write_text("return 1\n", encoding="utf-8")
    workspace = SnapshotWorkspace(original, tmp_path / "run" / "repo")
    workspace.create()
    with pytest.raises(WorkspacePreconditionError):
        workspace.apply_edit("app.py", "return 2\n")
    with pytest.raises(WorkspacePreconditionError):
        workspace.apply_edit("app.py", "return 2\n", expected_sha256="0" * 64)


def test_apply_edit_matches_crlf_files_line_ending_agnostically(tmp_path: Path):
    original = tmp_path / "repo"
    original.mkdir()
    source = original / "app.py"
    source.write_bytes(b"def run():\r\n    return 1\r\n")
    workspace = SnapshotWorkspace(original, tmp_path / "run" / "repo")
    workspace.create()
    result = workspace.apply_edit(
        "app.py",
        "    return 2\n",
        old_text="    return 1\n",
        reason="crlf-aware compact replacement",
    )
    staged = workspace.staging / "app.py"
    assert staged.read_bytes() == b"def run():\r\n    return 2\r\n"
    assert result["reason"] == "crlf-aware compact replacement"
    diff, changed = workspace.diff()
    assert changed == ["app.py"]
    assert "+    return 2" in diff
    assert "-    return 1" in diff


def test_argv_policy_never_auto_runs_unknown_or_composed_commands():
    assert classify_argv(["python", "-m", "pytest", "-q"]).allowed is True
    assert classify_argv(["python", "--version"]).allowed is True
    unknown = classify_argv(["python", "-c", "print(1)"])
    assert unknown.allowed is False
    assert unknown.requires_approval is True
    composed = classify_command("pytest -q && git push")
    assert composed.allowed is False
    assert composed.risk_level == "high"


def test_receipt_seal_and_verify():
    receipt = build_patch_receipt(
        task_id="task-1",
        goal="fix a bug",
        workspace={"kind": "snapshot", "baseline": {"files": 1}},
        model={"provider": "fake", "model": "smoke"},
        plan={"summary": "plan", "steps": [], "checks": []},
        tool_stats={"tool_calls": 3},
        changed_files=[{"path": "app.py", "before_sha256": "a", "after_sha256": "b"}],
        diff_hash="d" * 64,
        commands=[{"argv": ["python", "-m", "pytest", "-q"], "returncode": 0}],
        approvals=[],
        tests={"passed": True},
        event_chain_head="e" * 64,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:01:00+00:00",
        verdict="verified_pending_apply",
    )
    assert verify_receipt(receipt, receipt["receipt_hash"]) is True
    receipt["verdict"] = "tampered"
    assert verify_receipt(receipt) is False


def test_receipt_artifact_is_canonical_hashed_and_tamper_detected(tmp_path: Path):
    receipt = build_patch_receipt(
        task_id="artifact-task",
        goal="write receipt artifact",
        workspace={"kind": "snapshot"},
        model={"provider": "fake"},
        plan={"summary": "artifact", "steps": [], "checks": []},
        tool_stats={"tool_calls": 1},
        changed_files=[],
        diff_hash="d" * 64,
        commands=[],
        approvals=[],
        tests={"passed": True},
        event_chain_head="e" * 64,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:01:00+00:00",
        verdict="verified_pending_apply",
    )
    path, file_hash = write_receipt_atomic("artifact-task", receipt, root=tmp_path)
    assert path == tmp_path / "data" / "runs" / "artifact-task" / "receipt.json"
    assert verify_receipt_file(path, file_hash) is True
    path.write_text(path.read_text(encoding="utf-8").replace("verified_pending_apply", "tampered"), encoding="utf-8")
    assert verify_receipt_file(path, file_hash) is False


def test_process_executor_uses_timeout_and_truncates_output(tmp_path: Path):
    result = asyncio.run(
        ProcessExecutor(max_output_chars=20).run(
            ["python", "-c", "print('x' * 100)"],
            cwd=str(tmp_path),
            timeout_seconds=10,
        )
    )
    assert result.returncode == 0
    assert result.output_truncated is True
    assert len(result.stdout) <= 20
