from pathlib import Path

import pytest

from patchproof.workspace import SnapshotWorkspace


def test_apply_writes_only_after_snapshot_is_still_fresh(tmp_path: Path):
    original = tmp_path / "repo"
    original.mkdir()
    source = original / "app.py"
    source.write_text("return 1\n", encoding="utf-8")
    workspace = SnapshotWorkspace(original, tmp_path / "run" / "repo")

    workspace.create()
    workspace.safe_write("app.py", "return 2\n")
    assert workspace.apply() == ["app.py"]
    assert source.read_text(encoding="utf-8") == "return 2\n"


def test_apply_refuses_to_overwrite_manual_changes(tmp_path: Path):
    original = tmp_path / "repo"
    original.mkdir()
    source = original / "app.py"
    source.write_text("return 1\n", encoding="utf-8")
    workspace = SnapshotWorkspace(original, tmp_path / "run" / "repo")

    workspace.create()
    workspace.safe_write("app.py", "return 2\n")
    source.write_text("manual edit\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="真实仓库在任务期间发生变化"):
        workspace.apply()
