from pathlib import Path

from patchproof.repo_index import RepoIndex


def test_repo_index_finds_symbols_and_edges(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "from service import run\n\nclass App:\n    def start(self):\n        return run()\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    index = RepoIndex.build(tmp_path)
    assert {symbol.name for symbol in index.symbols} >= {"App", "start", "run"}
    assert any(edge["relation"] == "imports" for edge in index.edges)
    assert "service.py" in index.context_for("fix service run")


def test_repo_index_ignores_only_in_repo_excluded_dirs(tmp_path: Path):
    # A repository that lives under a ``data/`` directory must still be
    # indexed: the exclusion applies to directories inside the repository, not
    # to components of the checkout's own absolute path.
    data_root = tmp_path / "data" / "checkout"
    data_root.mkdir(parents=True)
    (data_root / "pkg.py").write_text("def fix():\n    return 1\n", encoding="utf-8")
    (data_root / "venv").mkdir()
    (data_root / "venv" / "lib.py").write_text("x = 1\n", encoding="utf-8")
    index = RepoIndex.build(data_root)
    assert index.files == ["pkg.py"]
    assert "fix" in index.context_for("repair fix")
