import hashlib
import subprocess
from pathlib import Path

import pytest

from patchproof.workspace import GitWorktreeWorkspace, SnapshotWorkspace, select_workspace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )


def test_dirty_repo_uses_snapshot_fallback(tmp_path: Path):
    repo = tmp_path / "dirty"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(repo, "add", "app.py").returncode == 0
    assert _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit",
        "-qm",
        "init",
    ).returncode == 0
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    workspace = select_workspace(repo, tmp_path / "run" / "repo")
    assert isinstance(workspace, SnapshotWorkspace)
    assert "dirty" in workspace.reason.lower()


def test_clean_repo_uses_git_worktree_and_stale_head_is_refused(tmp_path: Path):
    repo = tmp_path / "clean"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(repo, "add", "app.py").returncode == 0
    assert _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit",
        "-qm",
        "init",
    ).returncode == 0
    workspace = select_workspace(repo, tmp_path / "run" / "repo")
    if not isinstance(workspace, GitWorktreeWorkspace):
        pytest.skip(f"git worktree unavailable in environment: {workspace.reason}")
    workspace.create()
    current = (workspace.staging / "app.py").read_text(encoding="utf-8")
    workspace.apply_edit(
        "app.py",
        current.replace("1", "2"),
        expected_sha256=hashlib.sha256((workspace.staging / "app.py").read_bytes()).hexdigest(),
    )
    assert _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit",
        "--allow-empty",
        "-qm",
        "advance",
    ).returncode == 0
    with pytest.raises(RuntimeError, match="HEAD"):
        workspace.apply()
