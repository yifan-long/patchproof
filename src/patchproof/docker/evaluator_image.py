"""Controlled Docker evaluator image build and immutable lock artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..corpus.loader import CommandRunner, SubprocessCommandRunner

IMAGE_LOCK_SCHEMA = "patchproof.evaluator-image-lock.v1"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_TAG = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:\-]{0,254}$")


def is_immutable_image(value: str) -> bool:
    if value.startswith("sha256:"):
        return bool(_HEX64.fullmatch(value.removeprefix("sha256:")))
    if "@sha256:" not in value:
        return False
    return bool(_HEX64.fullmatch(value.rsplit("@sha256:", 1)[1]))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _result(raw: Any) -> tuple[int, str, str]:
    if isinstance(raw, dict):
        return int(raw.get("returncode", 1)), str(raw.get("stdout", "")), str(raw.get("stderr", ""))
    return int(raw.returncode), str(raw.stdout), str(raw.stderr)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class EvaluatorImageBuild:
    lock: dict[str, Any]
    build_argv: tuple[str, ...]
    inspect_argv: tuple[str, ...]


class EvaluatorImageBuilder:
    """Build an evaluator without Docker socket mounts or privileged flags."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        docker_cli: str = "docker",
        clock: Callable[[], datetime] | None = None,
    ):
        self.runner = runner or SubprocessCommandRunner()
        self.docker_cli = docker_cli
        self.clock = clock or (lambda: datetime.now(UTC))

    def build(
        self,
        *,
        context: str | Path,
        dockerfile: str | Path,
        base_image: str,
        tag: str,
        output: str | Path,
        requirements_lock: str | Path | None = None,
        acr_registry: str | None = None,
        pip_index_url: str | None = None,
        confirm_build: bool = False,
    ) -> EvaluatorImageBuild:
        if not confirm_build:
            raise ValueError("Docker image build requires --confirm-build")
        if not is_immutable_image(base_image):
            raise ValueError("base image must be pinned by digest or immutable image ID")
        context_path = Path(context).resolve()
        dockerfile_path = Path(dockerfile).resolve()
        if not context_path.is_dir() or not dockerfile_path.is_file():
            raise ValueError("Docker build context and Dockerfile must exist")
        try:
            dockerfile_path.relative_to(context_path)
        except ValueError as exc:
            raise ValueError("Dockerfile must be inside the controlled build context") from exc
        final_tag = self._mapped_tag(tag, acr_registry)
        argv = [
            self.docker_cli,
            "build",
            "--pull=false",
            "--file",
            str(dockerfile_path),
            "--build-arg",
            f"BASE_IMAGE={base_image}",
        ]
        if pip_index_url:
            self._validate_package_mirror(pip_index_url)
            argv.extend(["--build-arg", f"PIP_INDEX_URL={pip_index_url}"])
        argv.extend(["--tag", final_tag, str(context_path)])
        self._assert_safe(argv)
        code, _stdout, stderr = _result(self.runner.run(argv, shell=False, timeout_seconds=1800))
        if code != 0:
            raise RuntimeError(f"Docker evaluator build failed: {stderr[-2000:]}")

        inspect_argv = [self.docker_cli, "image", "inspect", final_tag]
        code, stdout, stderr = _result(self.runner.run(inspect_argv, shell=False, timeout_seconds=60))
        if code != 0:
            raise RuntimeError(f"Docker image inspect failed: {stderr[-2000:]}")
        try:
            inspected = json.loads(stdout)
            record = inspected[0] if isinstance(inspected, list) else inspected
            image_id = str(record["Id"])
            repo_digests = sorted(str(item) for item in record.get("RepoDigests") or [])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Docker inspect returned no verifiable image identity") from exc
        if not is_immutable_image(image_id):
            raise RuntimeError("Docker inspect image ID is not an immutable sha256 identity")
        immutable_reference = next((item for item in repo_digests if is_immutable_image(item)), image_id)
        runtime_argv = [
            self.docker_cli,
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            final_tag,
            "python",
            "-c",
            "import platform; print(platform.python_version())",
        ]
        self._assert_safe(runtime_argv)
        code, stdout, stderr = _result(self.runner.run(runtime_argv, shell=False, timeout_seconds=60))
        python_version = stdout.strip()
        if code != 0 or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", python_version):
            raise RuntimeError(f"Docker evaluator Python runtime probe failed: {stderr[-500:]}")
        requirements_path = Path(requirements_lock).resolve() if requirements_lock else None
        lock = {
            "schema_version": IMAGE_LOCK_SCHEMA,
            "built_at": self.clock().astimezone(UTC).isoformat(),
            "base_image": base_image,
            "requested_tag": tag,
            "local_tag": final_tag,
            "image_id": image_id,
            "repo_digests": repo_digests,
            "immutable_reference": immutable_reference,
            "dockerfile_sha256": _sha256(dockerfile_path),
            "requirements_lock_sha256": _sha256(requirements_path) if requirements_path else None,
            "runtime": {
                "python_version": python_version,
                "probe_argv": runtime_argv[-3:],
                "network": "none",
                "read_only": True,
            },
            "build_policy": {
                "pull": False,
                "docker_socket_mounted": False,
                "privileged": False,
                "acr_registry": acr_registry,
                "pip_index_host": urlparse(pip_index_url).hostname if pip_index_url else None,
                "daemon_config_mutated": False,
            },
        }
        canonical = json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        lock["lock_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
        _write_json(Path(output), lock)
        return EvaluatorImageBuild(lock, tuple(argv), tuple(inspect_argv))

    @staticmethod
    def _mapped_tag(tag: str, acr_registry: str | None) -> str:
        if not _TAG.fullmatch(tag) or "@" in tag or tag.endswith(":latest"):
            raise ValueError("evaluator tag is invalid or floating")
        if not acr_registry:
            return tag
        registry = acr_registry.strip().rstrip("/")
        if not _TAG.fullmatch(registry) or "://" in registry:
            raise ValueError("ACR registry must be a registry host/path, not a URL")
        return f"{registry}/{tag.rsplit('/', 1)[-1]}"

    @staticmethod
    def _validate_package_mirror(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("pip index must be an HTTPS package mirror")
        if "tuna" in parsed.hostname and parsed.hostname != "pypi.tuna.tsinghua.edu.cn":
            raise ValueError("TUNA pip mirror must use pypi.tuna.tsinghua.edu.cn")

    @staticmethod
    def _assert_safe(argv: Sequence[str]) -> None:
        forbidden = {"--privileged", "/var/run/docker.sock", "--mount", "-v", "--volume"}
        if any(item in forbidden or "docker.sock" in item for item in argv):
            raise ValueError("unsafe Docker image build option")


def load_evaluator_image_lock(path: str | Path) -> dict[str, Any]:
    lock = json.loads(Path(path).read_text(encoding="utf-8"))
    if lock.get("schema_version") != IMAGE_LOCK_SCHEMA:
        raise ValueError("unsupported evaluator image lock schema")
    if not is_immutable_image(str(lock.get("image_id", ""))):
        raise ValueError("image lock has no immutable local image ID")
    if not is_immutable_image(str(lock.get("immutable_reference", ""))):
        raise ValueError("image lock has no immutable evaluator reference")
    policy = lock.get("build_policy") or {}
    if policy.get("privileged") or policy.get("docker_socket_mounted") or policy.get("daemon_config_mutated"):
        raise ValueError("image lock records an unsafe build policy")
    expected_checksum = lock.get("lock_sha256")
    if not isinstance(expected_checksum, str) or not _HEX64.fullmatch(expected_checksum):
        raise ValueError("image lock has no valid checksum")
    payload = {key: value for key, value in lock.items() if key != "lock_sha256"}
    actual_checksum = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if actual_checksum != expected_checksum.lower():
        raise ValueError("evaluator image lock checksum mismatch")
    return lock
