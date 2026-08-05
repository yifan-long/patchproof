from pathlib import Path

import pytest

from patchproof.docker_executor import DockerEvalExecutor, DockerLimits, DockerUnavailableError


class FakeDockerRunner:
    def __init__(self, *, daemon=True):
        self.calls = []
        self.daemon = daemon

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if list(argv)[1:3] == ["version", "--format"]:
            return {"returncode": 0 if self.daemon else 127, "stdout": "27.0" if self.daemon else "", "stderr": ""}
        return {"returncode": 0 if self.daemon else 1, "stdout": "{}", "stderr": ""}


def test_docker_argv_has_isolation_flags_and_no_shell(tmp_path: Path):
    runner = FakeDockerRunner()
    executor = DockerEvalExecutor(
        image="python:3.12@sha256:" + "a" * 64,
        runner=runner,
        limits=DockerLimits(cpu=0.5, memory="512m", pids=32),
    )
    argv = executor.build_run_argv(["python", "-m", "pytest", "-q"], workspace=tmp_path)
    assert "--read-only" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in argv
    assert "--pids-limit" in argv
    assert "--privileged" not in argv
    assert "docker.sock" not in " ".join(argv)
    result = executor.preflight()
    assert result.daemon_available is True
    assert result.image_pinned is True
    assert runner.calls and runner.calls[0][1]["shell"] is False


def test_docker_rejects_unpinned_image_invalid_mount_and_local_fallback(tmp_path: Path):
    with pytest.raises(ValueError, match="pinned"):
        DockerEvalExecutor(image="python:3.12")
    executor = DockerEvalExecutor(image="python:3.12@sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="existing directory"):
        executor.build_run_argv(["python", "--version"], workspace=tmp_path / "missing")
    file_mount = tmp_path / "file"
    file_mount.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="existing directory"):
        executor.build_run_argv(["python", "--version"], workspace=file_mount)

    unavailable = DockerEvalExecutor(
        image="python:3.12@sha256:" + "a" * 64,
        runner=FakeDockerRunner(daemon=False),
    )
    assert unavailable.preflight().execution_mode == "unavailable"
    with pytest.raises(DockerUnavailableError):
        unavailable.run(["python", "--version"], workspace=tmp_path)


def test_local_fixture_is_explicitly_local_smoke_only(tmp_path: Path):
    runner = FakeDockerRunner(daemon=False)
    executor = DockerEvalExecutor(
        image="local://patchproof-python312",
        cache_root=tmp_path / "cache",
        runner=runner,
        registry="patchproof.azurecr.io",
    )
    state = executor.preflight().as_dict()
    assert state["execution_mode"] == "local_smoke_only"
    assert state["image_pinned"] is False
    assert state["registry"] == {"configured": True, "host": "patchproof.azurecr.io"}
    assert runner.calls[0][0][1:3] == ["version", "--format"]
