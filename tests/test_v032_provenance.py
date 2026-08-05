from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from patchproof.benchmark import main
from patchproof.corpus import SubprocessCommandRunner, build_fetch_plan, execute_fetch_plan, load_cases
from patchproof.docker_executor import DockerEvalExecutor
from patchproof.evaluator_image import EvaluatorImageBuilder, load_evaluator_image_lock
from patchproof.public_resolver import PublicProvenanceResolver, parse_official_run_test

FIXED_TIME = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
DATASET_URL = "https://official.test/BugsInPy"
SOURCE_URL = "https://official.test/example/project"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=True,
        timeout=20,
    )
    return result.stdout.strip()


def _fake_source(
    tmp_path: Path,
    *,
    with_license: bool = True,
    oracle_canary: str | None = None,
) -> tuple[Path, str, str]:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@patchproof.invalid")
    _git(repo, "config", "user.name", "PatchProof Tests")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    if with_license:
        (repo / "LICENSE").write_text(
            "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
            encoding="utf-8",
        )
    if oracle_canary:
        (repo / "bug_patch.txt").write_text(oracle_canary, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "buggy")
    buggy = _git(repo, "rev-parse", "HEAD")
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-m", "fixed")
    fixed = _git(repo, "rev-parse", "HEAD")
    return repo, buggy, fixed


def _descriptor(tmp_path: Path, buggy: str, fixed: str) -> tuple[Path, Path]:
    dataset = tmp_path / "dataset"
    bug_dir = dataset / "projects" / "demo" / "bugs" / "1"
    bug_dir.mkdir(parents=True)
    (bug_dir.parent.parent / "project.info").write_text(f'github_url="{SOURCE_URL}"\n', encoding="utf-8")
    (bug_dir / "bug.info").write_text(
        f'python_version="3.12.0"\n'
        f'buggy_commit_id="{buggy}"\nfixed_commit_id ="{fixed}"\n'
        'test_file="test_module.py"\n',
        encoding="utf-8",
    )
    (bug_dir / "run_test.sh").write_text("python -m unittest -q test_module.TestBug.test_value\n", encoding="utf-8")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(
        json.dumps(
            {
                "schema_version": "patchproof.corpus.v2",
                "dataset_url": DATASET_URL,
                "cases": [
                    {
                        "id": "bugsinpy-demo-1",
                        "suite": "public-bugsinpy",
                        "source_kind": "bugsinpy",
                        "repo_url": f"{DATASET_URL}/tree/master/projects/demo",
                        "project": "demo",
                        "bug_id": 1,
                        "source_url": f"{DATASET_URL}/README.md",
                        "issue": "Official fake identifier demo bug 1",
                        "goal": "Resolve provenance only",
                        "required_check_argv": ["python", "-m", "pytest", "-q"],
                        "privacy_public_code": True,
                        "provenance_state": "unresolved",
                        "resolver_requirements": ["resolve official metadata"],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return descriptor, dataset


def _image_lock(tmp_path: Path, python_version: str = "3.12.0") -> Path:
    path = tmp_path / "image.lock.json"
    payload = {
        "schema_version": "patchproof.evaluator-image-lock.v1",
        "image_id": "sha256:" + "a" * 64,
        "immutable_reference": "sha256:" + "a" * 64,
        "build_policy": {
            "privileged": False,
            "docker_socket_mounted": False,
            "daemon_config_mutated": False,
        },
        "runtime": {"python_version": python_version},
    }
    payload["lock_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


class FailingExecutionProbe:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        assert not (Path(kwargs["workspace"]) / "bug_patch.txt").exists()
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="AssertionError: expected 2",
            timed_out=False,
            cancelled=False,
        )


def test_local_official_metadata_resolves_with_verified_commit_license_and_image(tmp_path: Path) -> None:
    source, buggy, fixed = _fake_source(tmp_path)
    descriptor, dataset = _descriptor(tmp_path, buggy, fixed)
    original = descriptor.read_bytes()
    output = tmp_path / "resolved.lock.json"
    report = PublicProvenanceResolver(
        tmp_path / "cache",
        runner=SubprocessCommandRunner(),
        clock=lambda: FIXED_TIME,
        dataset_root=dataset,
        dataset_revision="d" * 40,
        repository_overrides={SOURCE_URL: source},
        allowed_dataset_urls={DATASET_URL},
        execution_probe=FailingExecutionProbe(),
    ).resolve(descriptor, output=output, image_lock=_image_lock(tmp_path), confirm_download=False)

    assert descriptor.read_bytes() == original
    assert report["resolutions"][0]["status"] == "resolved"
    case = load_cases(output, allow_unresolved=False)[0]
    assert case.immutable_revision == buggy
    assert case.license_spdx == "MIT"
    assert case.image == "sha256:" + "a" * 64
    assert case.python_version == "3.12.0"
    assert case.test_file == "test_module.py"
    assert case.executable_state == "verified_failing"
    assert case.required_check_argv == [
        "python",
        "-m",
        "unittest",
        "-q",
        "test_module.TestBug.test_value",
    ]
    assert "official failing test" in case.goal
    # test_module.py is absent from the buggy snapshot, so the resolver orients
    # the model toward the library code instead of the missing test file.
    assert "is introduced by the fix" in case.goal
    assert case.expected_contents == {}
    assert case.assertions == []
    assert report["resolutions"][0]["provenance"]["fixed_commit"] == fixed
    assert report["resolutions"][0]["provenance"]["source"]["head_verified"] is True
    assert len(report["resolutions"][0]["provenance"]["run_test"]["sha256"]) == 64
    assert report["model_calls"] == 0
    assert report["public_code_llm_egress"] is False
    assert len(report["reproducibility_sha256"]) == 64
    plan = build_fetch_plan(case, tmp_path / "cache", confirm_download=False)
    assert plan.status == "cached"
    assert plan.evidence["cache_kind"] == "resolved_source"
    fetched = execute_fetch_plan(plan, runner=SubprocessCommandRunner(), confirm_download=False)
    assert fetched["status"] == "ready"
    assert fetched["revision_verified"] is True


def test_goal_hint_is_omitted_when_the_failing_test_file_exists_in_buggy_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "upstream"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "tests@patchproof.invalid")
    _git(source, "config", "user.name", "PatchProof Tests")
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "test_module.py").write_text(
        "class TestBug:\n    def test_value(self):\n        pass\n", encoding="utf-8"
    )
    (source / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "buggy")
    buggy = _git(source, "rev-parse", "HEAD")
    (source / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(source, "add", "module.py")
    _git(source, "commit", "-m", "fixed")
    fixed = _git(source, "rev-parse", "HEAD")
    descriptor, dataset = _descriptor(tmp_path, buggy, fixed)
    output = tmp_path / "resolved-with-test.lock.json"
    report = PublicProvenanceResolver(
        tmp_path / "cache",
        runner=SubprocessCommandRunner(),
        clock=lambda: FIXED_TIME,
        dataset_root=dataset,
        dataset_revision="d" * 40,
        repository_overrides={SOURCE_URL: source},
        allowed_dataset_urls={DATASET_URL},
        execution_probe=FailingExecutionProbe(),
    ).resolve(descriptor, output=output, image_lock=_image_lock(tmp_path), confirm_download=False)

    assert report["resolutions"][0]["status"] == "resolved"
    case = load_cases(output, allow_unresolved=False)[0]
    assert "official failing test" in case.goal
    assert "is introduced by the fix" not in case.goal


def test_official_run_test_accepts_one_safe_argv_and_rejects_shell_features(tmp_path: Path) -> None:
    run_test = tmp_path / "run_test.sh"
    run_test.write_text(
        "python -m unittest -q test.test_InfoExtractor.TestInfoExtractor.test_parse_mpd_formats\n",
        encoding="utf-8",
    )
    assert parse_official_run_test(run_test) == [
        "python",
        "-m",
        "unittest",
        "-q",
        "test.test_InfoExtractor.TestInfoExtractor.test_parse_mpd_formats",
    ]

    for unsafe in (
        "python -m unittest test_x && echo bad\n",
        "TOKEN=secret python -m unittest test_x\n",
        "sh -c 'python -m unittest test_x'\n",
        "python -m unittest test_x\npython -m unittest test_y\n",
        "python -m unittest test_x > result.txt\n",
    ):
        run_test.write_text(unsafe, encoding="utf-8")
        with pytest.raises(ValueError):
            parse_official_run_test(run_test)


def test_oracle_canary_is_never_materialized_or_serialized(tmp_path: Path) -> None:
    canary = "PATCHPROOF_ORACLE_CANARY_DO_NOT_EXPOSE"
    source, buggy, fixed = _fake_source(tmp_path, oracle_canary=canary)
    descriptor, dataset = _descriptor(tmp_path, buggy, fixed)
    bug_dir = dataset / "projects" / "demo" / "bugs" / "1"
    (bug_dir / "bug_patch.txt").write_text(canary, encoding="utf-8")
    probe = FailingExecutionProbe()
    report = PublicProvenanceResolver(
        tmp_path / "cache",
        dataset_root=dataset,
        dataset_revision="d" * 40,
        repository_overrides={SOURCE_URL: source},
        allowed_dataset_urls={DATASET_URL},
        execution_probe=probe,
        clock=lambda: FIXED_TIME,
    ).resolve(
        descriptor,
        output=tmp_path / "resolved.lock.json",
        image_lock=_image_lock(tmp_path),
        confirm_download=False,
    )

    assert report["resolutions"][0]["status"] == "resolved"
    assert canary not in json.dumps(report)
    assert "bug_patch.txt" not in json.dumps(report)
    assert len(probe.calls) == 1


def test_runtime_version_mismatch_remains_environment_unreproducible(tmp_path: Path) -> None:
    source, buggy, fixed = _fake_source(tmp_path)
    descriptor, dataset = _descriptor(tmp_path, buggy, fixed)
    probe = FailingExecutionProbe()
    report = PublicProvenanceResolver(
        tmp_path / "cache",
        dataset_root=dataset,
        dataset_revision="d" * 40,
        repository_overrides={SOURCE_URL: source},
        allowed_dataset_urls={DATASET_URL},
        execution_probe=probe,
        clock=lambda: FIXED_TIME,
    ).resolve(
        descriptor,
        output=tmp_path / "unresolved.lock.json",
        image_lock=_image_lock(tmp_path, "3.11.9"),
        confirm_download=False,
    )

    resolution = report["resolutions"][0]
    assert resolution["status"] == "unresolved"
    assert "runtime_version_mismatch" in {item["code"] for item in resolution["reasons"]}
    assert resolution["case_id"] == "bugsinpy-demo-1"
    assert probe.calls == []


def test_ambiguous_license_remains_unresolved_with_structured_reason(tmp_path: Path) -> None:
    source, buggy, fixed = _fake_source(tmp_path, with_license=False)
    descriptor, dataset = _descriptor(tmp_path, buggy, fixed)
    output = tmp_path / "unresolved.lock.json"
    report = PublicProvenanceResolver(
        tmp_path / "cache",
        dataset_root=dataset,
        dataset_revision="d" * 40,
        repository_overrides={SOURCE_URL: source},
        allowed_dataset_urls={DATASET_URL},
        clock=lambda: FIXED_TIME,
        execution_probe=FailingExecutionProbe(),
    ).resolve(descriptor, output=output, image_lock=_image_lock(tmp_path), confirm_download=False)

    assert report["resolutions"][0]["status"] == "unresolved"
    assert "license_unresolved" in {item["code"] for item in report["resolutions"][0]["reasons"]}
    with pytest.raises(ValueError, match="unresolved"):
        load_cases(output, allow_unresolved=False)


class FakeDockerRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs):
        assert kwargs["shell"] is False
        self.calls.append(list(argv))
        if list(argv)[1:3] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"Id": "sha256:" + "c" * 64, "RepoDigests": []}]),
                stderr="",
            )
        if len(argv) > 1 and argv[1] == "run":
            return SimpleNamespace(returncode=0, stdout="3.12.7\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="built", stderr="")


class FlakyRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, argv, **kwargs):
        assert kwargs["shell"] is False
        self.calls += 1
        return SimpleNamespace(
            returncode=0 if self.calls == 2 else 1,
            stdout="verified" if self.calls == 2 else "",
            stderr="transient" if self.calls == 1 else "",
        )


def test_evaluator_builder_uses_safe_argv_acr_and_tuna_roles(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    requirements = context / "requirements.lock"
    dockerfile.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
    requirements.write_text("pytest==8.4.2\n", encoding="utf-8")
    runner = FakeDockerRunner()
    output = tmp_path / "evaluator.lock.json"
    result = EvaluatorImageBuilder(runner=runner, clock=lambda: FIXED_TIME).build(
        context=context,
        dockerfile=dockerfile,
        requirements_lock=requirements,
        base_image="python@sha256:" + "d" * 64,
        tag="patchproof-evaluator:0.3.2",
        acr_registry="registry.cn-hangzhou.aliyuncs.com/patchproof",
        pip_index_url="https://pypi.tuna.tsinghua.edu.cn/simple",
        output=output,
        confirm_build=True,
    )

    build = list(result.build_argv)
    assert "--privileged" not in build
    assert not any("docker.sock" in item for item in build)
    assert "registry.cn-hangzhou.aliyuncs.com/patchproof/patchproof-evaluator:0.3.2" in build
    assert "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in build
    assert load_evaluator_image_lock(output)["immutable_reference"] == "sha256:" + "c" * 64


def test_build_rejects_unpinned_base_and_resolver_cli_requires_confirmation(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="base image"):
        EvaluatorImageBuilder(runner=FakeDockerRunner()).build(
            context=tmp_path,
            dockerfile=tmp_path / "missing",
            base_image="python:3.12",
            tag="patchproof-evaluator:0.3.2",
            output=tmp_path / "lock.json",
            confirm_build=True,
        )
    monkeypatch.setattr(
        sys,
        "argv",
        ["patchproof.benchmark", "resolve-public", "--manifest", "unused.json", "--output", "unused.lock.json"],
    )
    with pytest.raises(SystemExit, match="confirm-download"):
        main()


def test_image_lock_tamper_is_rejected(tmp_path: Path) -> None:
    lock = _image_lock(tmp_path)
    payload = json.loads(lock.read_text(encoding="utf-8"))
    payload["image_id"] = "sha256:" + "e" * 64
    lock.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_evaluator_image_lock(lock)


def test_docker_executor_accepts_inspected_immutable_image_id(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = DockerEvalExecutor(image="sha256:" + "f" * 64, runner=FakeDockerRunner())
    argv = executor.build_run_argv(["python", "-m", "pytest", "-q"], workspace=workspace)
    assert "sha256:" + "f" * 64 in argv
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"


def test_resolver_retries_bounded_transient_command_failure(tmp_path: Path) -> None:
    runner = FlakyRunner()
    resolver = PublicProvenanceResolver(tmp_path / "cache", runner=runner, retries=3)
    code, stdout, _stderr = resolver._run_retry(["git", "version"], timeout=5)
    assert code == 0
    assert stdout == "verified"
    assert runner.calls == 2
