"""Opt-in, evidence-preserving resolver for official BugsInPy descriptors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .artifact_policy import copytree_without_oracles, is_denied_artifact
from .corpus import CommandRunner, SubprocessCommandRunner, load_cases
from .evaluator_image import load_evaluator_image_lock
from .models import BenchmarkCase
from .policy import classify_argv

PUBLIC_LOCK_SCHEMA = "patchproof.public-lock.v2"
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_OFFICIAL_DATASET = "https://github.com/soarsmu/BugsInPy"
_PYTHON_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class DatasetSnapshot:
    root: Path
    url: str
    revision: str
    cache_key: str
    content_sha256: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_hash(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def _tree_hash(root: Path, *, ignored: set[str] | None = None) -> str:
    ignored = ignored or {".git"}
    digest = hashlib.sha256()
    paths = (
        item
        for item in root.rglob("*")
        if (item.is_file() or item.is_symlink())
        and not ignored.intersection(item.parts)
        and not is_denied_artifact(item.relative_to(root))
    )
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = ("SYMLINK:" + os.readlink(path)).encode() if path.is_symlink() else path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _command_result(raw: Any) -> tuple[int, str, str]:
    if isinstance(raw, dict):
        return int(raw.get("returncode", 1)), str(raw.get("stdout", "")), str(raw.get("stderr", ""))
    return int(raw.returncode), str(raw.stdout), str(raw.stderr)


def _parse_info(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "=" if "=" in line else ":" if ":" in line else None
        if separator is None:
            raise ValueError(f"unsupported metadata line {number} in {path.name}")
        key, value = (item.strip() for item in line.split(separator, 1))
        normalized = key.lower().replace("-", "_")
        if value[:1] in {'"', "'"}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"unterminated quoted value on line {number} in {path.name}")
            value = value[1:-1].strip()
        if not normalized or not value:
            raise ValueError(f"empty metadata key/value on line {number} in {path.name}")
        if normalized in values and values[normalized] != value:
            raise ValueError(f"ambiguous duplicate metadata key {normalized}")
        values[normalized] = value
    return values


def _pick(values: Mapping[str, str], names: Sequence[str]) -> str | None:
    found = {values[name].strip() for name in names if values.get(name, "").strip()}
    if len(found) > 1:
        raise ValueError(f"ambiguous metadata values for {', '.join(names)}")
    return next(iter(found), None)


def _canonical_repo_url(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("git@github.com:"):
        candidate = "https://github.com/" + candidate.removeprefix("git@github.com:")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("official upstream repository URL must be credential-free HTTPS")
    return candidate.rstrip("/")


def _parse_argv(value: str) -> list[str]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        raw = shlex.split(value, posix=True)
    argv = raw if isinstance(raw, list) else []
    if not argv or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise ValueError("official command metadata is not a non-empty argv")
    forbidden = {"sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"}
    if _ENV_ASSIGNMENT.match(argv[0]) or Path(argv[0]).name.lower() in forbidden or any(
        token in item for item in argv for token in ("&&", "||", "\n", "\r", ">", "<", ";", "|", "`", "$(")
    ):
        raise ValueError("official command metadata requires shell composition")
    decision = classify_argv(argv)
    if not decision.allowed or decision.requires_approval:
        raise ValueError(f"official command is outside the safe check policy: {decision.reason}")
    return argv


def parse_official_run_test(path: Path) -> list[str]:
    """Parse an official run_test.sh only when it is exactly one safe command."""

    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("run_test.sh must contain exactly one nonempty command line")
    return _parse_argv(lines[0])


def _major_minor(value: str) -> tuple[int, int]:
    parts = value.split(".")
    return int(parts[0]), int(parts[1])


def _execution_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    as_dict = getattr(raw, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return {
        "returncode": int(getattr(raw, "returncode", 1)),
        "stdout": str(getattr(raw, "stdout", "")),
        "stderr": str(getattr(raw, "stderr", "")),
        "timed_out": bool(getattr(raw, "timed_out", False)),
        "cancelled": bool(getattr(raw, "cancelled", False)),
    }


def _looks_environment_unreproducible(result: Mapping[str, Any]) -> bool:
    output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".lower()
    markers = (
        "no module named",
        "modulenotfounderror",
        "importerror",
        "command not found",
        "can't open file",
        "cannot open file",
        "syntaxerror",
    )
    return bool(result.get("timed_out") or result.get("cancelled") or any(marker in output for marker in markers))


def _license(path: Path) -> tuple[str | None, dict[str, Any]]:
    candidates = sorted(
        item
        for item in path.iterdir()
        if item.is_file() and not item.is_symlink() and item.name.lower().split(".", 1)[0] in {"license", "copying"}
    )
    if not candidates:
        return None, {"status": "unresolved", "reason": "license_file_missing"}
    matches: set[str] = set()
    evidence: list[dict[str, str]] = []
    for candidate in candidates:
        text = candidate.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        spdx_lines = re.findall(r"SPDX-License-Identifier:\s*([A-Za-z0-9.+-]+)", text, flags=re.IGNORECASE)
        detected: str | None = spdx_lines[0] if len(set(spdx_lines)) == 1 else None
        if not detected and "permission is hereby granted, free of charge" in lowered:
            detected = "MIT"
        elif not detected and "apache license" in lowered and "version 2.0" in lowered:
            detected = "Apache-2.0"
        elif not detected and "free and unencumbered software released into the public domain" in lowered:
            detected = "Unlicense"
        elif not detected and "redistribution and use in source and binary forms" in lowered:
            detected = "BSD-3-Clause" if "neither the name" in lowered else "BSD-2-Clause"
        if detected:
            matches.add(detected)
        evidence.append({"path": candidate.name, "sha256": _file_hash(candidate), "detected_spdx": detected or ""})
    if len(matches) != 1:
        return None, {"status": "unresolved", "reason": "license_ambiguous", "files": evidence}
    return next(iter(matches)), {"status": "resolved", "method": "spdx_or_conservative_text_match", "files": evidence}


class PublicProvenanceResolver:
    """Resolve public descriptors without exposing code to a model."""

    def __init__(
        self,
        cache_root: str | Path,
        *,
        runner: CommandRunner | None = None,
        retries: int = 3,
        clock: Callable[[], datetime] | None = None,
        dataset_root: str | Path | None = None,
        dataset_revision: str | None = None,
        repository_overrides: Mapping[str, str | Path] | None = None,
        allowed_dataset_urls: set[str] | None = None,
        execution_probe: Any | None = None,
    ):
        self.cache_root = Path(cache_root).resolve()
        self.runner = runner or SubprocessCommandRunner()
        self.retries = max(1, min(retries, 5))
        self.clock = clock or (lambda: datetime.now(UTC))
        self.dataset_root = Path(dataset_root).resolve() if dataset_root else None
        self.dataset_revision = dataset_revision
        self.repository_overrides = {
            key.rstrip("/"): Path(value).resolve() for key, value in (repository_overrides or {}).items()
        }
        self.allowed_dataset_urls = allowed_dataset_urls or {_OFFICIAL_DATASET}
        self.execution_probe = execution_probe

    def resolve(
        self,
        descriptor: str | Path,
        *,
        output: str | Path,
        image_lock: str | Path | None,
        confirm_download: bool = False,
    ) -> dict[str, Any]:
        descriptor_path = Path(descriptor).resolve()
        descriptor_raw = json.loads(descriptor_path.read_text(encoding="utf-8"))
        source_before = descriptor_path.read_bytes()
        cases = load_cases(descriptor_path)
        dataset_url = str(descriptor_raw.get("dataset_url", "")).rstrip("/")
        generated_at = self.clock().astimezone(UTC).isoformat()
        image: dict[str, Any] | None = None
        image_error: str | None = None
        if image_lock:
            try:
                image = load_evaluator_image_lock(image_lock)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                image_error = str(exc)
        else:
            image_error = "evaluator_image_lock_missing"

        snapshot: DatasetSnapshot | None = None
        dataset_error: str | None = None
        if dataset_url not in self.allowed_dataset_urls:
            dataset_error = "dataset_url_not_allowlisted"
        elif self.dataset_root:
            if not self.dataset_revision or not _COMMIT.fullmatch(self.dataset_revision):
                dataset_error = "injected_dataset_revision_not_immutable"
            elif not self.dataset_root.is_dir():
                dataset_error = "injected_dataset_root_missing"
            else:
                snapshot = DatasetSnapshot(
                    self.dataset_root,
                    dataset_url,
                    self.dataset_revision.lower(),
                    _hash_bytes(f"{dataset_url}@{self.dataset_revision.lower()}".encode()),
                    _tree_hash(self.dataset_root),
                )
        elif not confirm_download:
            dataset_error = "confirm_download_required"
        else:
            try:
                snapshot = self._fetch_dataset(dataset_url)
            except (OSError, RuntimeError, ValueError) as exc:
                dataset_error = str(exc)

        resolved_cases: list[dict[str, Any]] = []
        resolutions: list[dict[str, Any]] = []
        for case in cases:
            clean_case = case.without_oracle()
            if clean_case.source_kind != "bugsinpy":
                record = self._unresolved(clean_case, "unsupported_source_kind", generated_at)
            elif snapshot is None:
                record = self._unresolved(clean_case, dataset_error or "dataset_unavailable", generated_at)
            else:
                record = self._resolve_case(clean_case, snapshot, image, image_error, confirm_download)
            resolved_cases.append(record.pop("case"))
            resolutions.append(record)

        if descriptor_path.read_bytes() != source_before:
            raise RuntimeError("source descriptor changed during resolution")
        reproducible = {
            "descriptor_sha256": _hash_bytes(source_before),
            "dataset_revision": snapshot.revision if snapshot else None,
            "dataset_content_sha256": snapshot.content_sha256 if snapshot else None,
            "image_lock_sha256": image.get("lock_sha256") if image else None,
            "cases": resolved_cases,
            "resolutions": [
                {key: value for key, value in record.items() if key != "resolved_at"}
                for record in resolutions
            ],
        }
        manifest = {
            "schema_version": PUBLIC_LOCK_SCHEMA,
            "generated_at": generated_at,
            "descriptor": {
                "path": descriptor_path.name,
                "sha256": _hash_bytes(source_before),
                "dataset_url": dataset_url,
            },
            "dataset": {
                "status": "resolved" if snapshot else "unresolved",
                "revision": snapshot.revision if snapshot else None,
                "cache_key": snapshot.cache_key if snapshot else None,
                "content_sha256": snapshot.content_sha256 if snapshot else None,
                "reason": dataset_error,
            },
            "evaluator_image": image,
            "cases": resolved_cases,
            "resolutions": resolutions,
            "reproducibility_sha256": _hash_bytes(_canonical_json(reproducible).encode()),
            "model_calls": 0,
            "public_code_llm_egress": False,
        }
        _write_json(Path(output), manifest)
        return manifest

    def _fetch_dataset(self, url: str) -> DatasetSnapshot:
        code, stdout, stderr = self._run_retry(["git", "ls-remote", url, "refs/heads/master"], timeout=120)
        if code != 0:
            raise RuntimeError(f"official_dataset_ls_remote_failed:{stderr[-500:]}")
        rows = [line.split() for line in stdout.splitlines() if line.strip()]
        revisions = {row[0].lower() for row in rows if len(row) >= 2 and _COMMIT.fullmatch(row[0])}
        if len(revisions) != 1:
            raise ValueError("official_dataset_revision_ambiguous")
        revision = next(iter(revisions))
        key = _hash_bytes(f"{url}@{revision}".encode())
        destination = self.cache_root / "official" / "bugsinpy" / key
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            code, _stdout, stderr = self._run_retry(
                ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(destination)], timeout=600
            )
            if code != 0:
                raise RuntimeError(f"official_dataset_clone_failed:{stderr[-500:]}")
        self._checkout(destination, revision)
        return DatasetSnapshot(destination, url, revision, key, _tree_hash(destination))

    def _resolve_case(
        self,
        case: BenchmarkCase,
        dataset: DatasetSnapshot,
        image: dict[str, Any] | None,
        image_error: str | None,
        confirm_download: bool,
    ) -> dict[str, Any]:
        resolved_at = self.clock().astimezone(UTC).isoformat()
        reasons: list[dict[str, str]] = []
        if not case.project or not _SAFE_NAME.fullmatch(case.project) or case.bug_id is None:
            return self._unresolved(case, "invalid_project_or_bug_identifier", resolved_at)
        project_dir = dataset.root / "projects" / case.project
        bug_dir = project_dir / "bugs" / str(case.bug_id)
        project_info_path = project_dir / "project.info"
        bug_info_path = bug_dir / "bug.info"
        run_test_path = bug_dir / "run_test.sh"
        if not project_info_path.is_file() or not bug_info_path.is_file() or not run_test_path.is_file():
            return self._unresolved(case, "official_metadata_missing", resolved_at)
        try:
            project_info = _parse_info(project_info_path)
            bug_info = _parse_info(bug_info_path)
            repo_url = _canonical_repo_url(
                _pick(project_info, ("github_url", "repo_url", "repository_url", "repository")) or ""
            )
            buggy = _pick(bug_info, ("buggy_commit_id", "buggy_commit", "bug_commit_id"))
            fixed = _pick(bug_info, ("fixed_commit_id", "fixed_commit", "fix_commit_id"))
            python_version = _pick(bug_info, ("python_version",))
            test_file = _pick(bug_info, ("test_file", "test_path"))
        except ValueError as exc:
            return self._unresolved(case, f"metadata_ambiguous:{exc}", resolved_at)
        if not buggy or not _COMMIT.fullmatch(buggy):
            reasons.append({"code": "buggy_commit_unresolved", "message": "official metadata lacks a full commit"})
        if not fixed or not _COMMIT.fullmatch(fixed):
            reasons.append({"code": "fixed_commit_unresolved", "message": "official metadata lacks a full commit"})

        if not python_version or not _PYTHON_VERSION.fullmatch(python_version):
            reasons.append({"code": "python_version_unresolved", "message": "official metadata lacks Python version"})
        if not test_file:
            reasons.append({"code": "test_file_unresolved", "message": "official metadata lacks failing test path"})

        check_argv = case.required_check_argv
        check_source = "descriptor_fallback"
        try:
            check_argv = parse_official_run_test(run_test_path)
            check_source = "official_run_test"
        except ValueError as exc:
            reasons.append({"code": "official_command_unsafe", "message": str(exc)})

        setup_argv = case.setup_argv
        command_sources = {"setup": "descriptor", "test": check_source}
        try:
            official_setup = _pick(project_info | bug_info, ("setup_argv", "setup_cmd", "install_cmd"))
            if official_setup:
                setup_argv = [_parse_argv(official_setup)]
                command_sources["setup"] = "official_metadata"
        except ValueError as exc:
            reasons.append({"code": "official_command_unsafe", "message": str(exc)})

        source_evidence: dict[str, Any] = {}
        spdx: str | None = None
        license_evidence: dict[str, Any] = {"status": "unresolved", "reason": "source_not_checked_out"}
        checkout: Path | None = None
        if buggy and _COMMIT.fullmatch(buggy):
            if not confirm_download and repo_url not in self.repository_overrides:
                reasons.append(
                    {"code": "confirm_download_required", "message": "upstream checkout requires confirmation"}
                )
            else:
                try:
                    checkout = self._fetch_source(repo_url, buggy.lower())
                    spdx, license_evidence = _license(checkout)
                    source_evidence = {
                        "cache_key": _hash_bytes(f"{repo_url}@{buggy.lower()}".encode()),
                        "content_sha256": _tree_hash(checkout),
                        "head": buggy.lower(),
                        "head_verified": True,
                    }
                except (OSError, RuntimeError, ValueError) as exc:
                    reasons.append({"code": "source_checkout_failed", "message": str(exc)})
        test_file_missing_in_buggy = bool(
            checkout is not None and test_file and not (checkout / test_file).exists()
        )
        goal_text = (
            f"Repair BugsInPy {case.project} bug {case.bug_id}; official failing test: "
            f"{check_argv[-1] if check_source == 'official_run_test' else test_file} ({test_file})."
        )
        if test_file_missing_in_buggy:
            goal_text += (
                " The official failing test file is absent in the buggy snapshot and is introduced by the fix; "
                "repair the underlying library code so the official check passes."
            )
        if not spdx:
            reasons.append({"code": "license_unresolved", "message": str(license_evidence.get("reason", "ambiguous"))})
        if not image:
            reasons.append({"code": "evaluator_image_unresolved", "message": image_error or "missing image lock"})

        executable_state = "unverified"
        execution_evidence: dict[str, Any] = {"status": "unverified"}
        image_python = str(((image or {}).get("runtime") or {}).get("python_version") or "")
        if python_version and _PYTHON_VERSION.fullmatch(python_version):
            if not image_python or not _PYTHON_VERSION.fullmatch(image_python):
                reasons.append(
                    {"code": "evaluator_runtime_unverified", "message": "image lock has no verified Python runtime"}
                )
            elif _major_minor(image_python) != _major_minor(python_version):
                executable_state = "environment_unreproducible"
                reasons.append(
                    {
                        "code": "runtime_version_mismatch",
                        "message": f"case requires Python {python_version}; evaluator provides {image_python}",
                    }
                )
                execution_evidence = {
                    "status": executable_state,
                    "required_python": python_version,
                    "image_python": image_python,
                }
            elif source_evidence and image:
                if self.execution_probe is None:
                    reasons.append(
                        {
                            "code": "execution_probe_unavailable",
                            "message": "pinned-image failing check was not verified",
                        }
                    )
                else:
                    executable_state, execution_evidence = self._probe_case(
                        checkout,
                        check_argv,
                        image["immutable_reference"],
                        case.timeout,
                    )
                    if executable_state != "verified_failing":
                        reasons.append(
                            {"code": executable_state, "message": "official check did not yield a reproducible failure"}
                        )

        metadata_url = (
            f"{dataset.url}/tree/{dataset.revision}/projects/{case.project}/bugs/{case.bug_id}"
        )
        provenance = {
            "dataset_url": dataset.url,
            "dataset_revision": dataset.revision,
            "official_metadata_url": metadata_url,
            "project_info": {
                "path": project_info_path.relative_to(dataset.root).as_posix(),
                "sha256": _file_hash(project_info_path),
            },
            "bug_info": {
                "path": bug_info_path.relative_to(dataset.root).as_posix(),
                "sha256": _file_hash(bug_info_path),
            },
            "run_test": {
                "path": run_test_path.relative_to(dataset.root).as_posix(),
                "sha256": _file_hash(run_test_path),
            },
            "task": {
                "python_version": python_version,
                "test_file": test_file,
                "check_argv": check_argv,
            },
            "canonical_source_url": repo_url,
            "buggy_commit": buggy,
            "fixed_commit": fixed,
            "source": source_evidence,
            "license": license_evidence,
            "commands": command_sources,
            "evaluator_image_lock_sha256": image.get("lock_sha256") if image else None,
            "execution": execution_evidence,
            "oracle_artifacts": {"policy": "excluded_without_reading", "excluded_count": sum(
                1 for item in bug_dir.iterdir() if is_denied_artifact(item.name)
            )},
        }
        if reasons:
            unresolved = case.model_copy(
                update={
                    "repo_url": repo_url,
                    "source_url": metadata_url,
                    "immutable_revision": None,
                    "license_spdx": None,
                    "image": None,
                    "provenance_state": "unresolved",
                    "resolver_requirements": sorted({item["code"] for item in reasons}),
                    "expected_contents": {},
                    "assertions": [],
                    "python_version": python_version,
                    "test_file": test_file,
                    "executable_state": executable_state,
                    "setup_argv": setup_argv,
                    "required_check_argv": check_argv,
                    "goal": goal_text,
                }
            )
            return {
                "case": unresolved.model_dump(mode="json"),
                "case_id": case.id,
                "status": "unresolved",
                "reasons": reasons,
                "provenance": provenance,
                "resolved_at": resolved_at,
            }
        resolved = BenchmarkCase.model_validate(
            case.model_copy(
                update={
                    "repo_url": repo_url,
                    "immutable_revision": buggy.lower(),
                    "license_spdx": spdx,
                    "source_url": metadata_url,
                    "setup_argv": setup_argv,
                    "required_check_argv": check_argv,
                    "goal": goal_text,
                    "python_version": python_version,
                    "test_file": test_file,
                    "executable_state": "verified_failing",
                    "image": image["immutable_reference"],
                    "provenance_state": "resolved",
                    "resolver_requirements": [],
                    "expected_contents": {},
                    "assertions": [],
                }
            ).model_dump(mode="json")
        )
        return {
            "case": resolved.model_dump(mode="json"),
            "case_id": case.id,
            "status": "resolved",
            "reasons": [],
            "provenance": provenance,
            "resolved_at": resolved_at,
        }

    def _probe_case(
        self,
        checkout: Path,
        check_argv: list[str],
        image: str,
        timeout_seconds: int,
    ) -> tuple[str, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="patchproof-resolver-probe-") as directory:
            workspace = Path(directory) / "repo"
            copytree_without_oracles(checkout, workspace)
            raw = self.execution_probe.run(
                check_argv,
                workspace=workspace,
                image=image,
                timeout_seconds=timeout_seconds,
                output_limit=4000,
            )
            result = _execution_result(raw)
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        evidence = {
            "status": "unverified",
            "returncode": int(result.get("returncode", 1)),
            "stdout_sha256": _hash_bytes(stdout.encode()),
            "stderr_sha256": _hash_bytes(stderr.encode()),
            "timed_out": bool(result.get("timed_out", False)),
            "cancelled": bool(result.get("cancelled", False)),
            "image": image,
        }
        if int(result.get("returncode", 1)) == 0:
            evidence["status"] = "already_passing"
            return "already_passing", evidence
        if _looks_environment_unreproducible(result):
            evidence["status"] = "environment_unreproducible"
            return "environment_unreproducible", evidence
        evidence["status"] = "verified_failing"
        return "verified_failing", evidence

    def _fetch_source(self, canonical_url: str, revision: str) -> Path:
        key = _hash_bytes(f"{canonical_url}@{revision}".encode())
        destination = self.cache_root / "sources" / key
        fetch_url = str(self.repository_overrides.get(canonical_url, canonical_url))
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            code, _stdout, stderr = self._run_retry(
                ["git", "clone", "--no-checkout", fetch_url, str(destination)], timeout=600
            )
            if code != 0:
                raise RuntimeError(f"source_clone_failed:{stderr[-500:]}")
        self._checkout(destination, revision)
        return destination

    def _checkout(self, destination: Path, revision: str) -> None:
        code, _stdout, stderr = self._run_retry(
            ["git", "-C", str(destination), "checkout", "--detach", revision], timeout=180
        )
        if code != 0:
            raise RuntimeError(f"checkout_failed:{stderr[-500:]}")
        code, stdout, stderr = self._run_retry(["git", "-C", str(destination), "rev-parse", "HEAD"], timeout=60)
        if code != 0 or stdout.strip().lower() != revision.lower():
            raise RuntimeError(f"checked_out_head_mismatch:{stderr[-500:]}")

    def _run_retry(self, argv: Sequence[str], *, timeout: int) -> tuple[int, str, str]:
        last = (1, "", "not attempted")
        for _attempt in range(self.retries):
            last = _command_result(self.runner.run(list(argv), shell=False, timeout_seconds=timeout))
            if last[0] == 0:
                break
        return last

    @staticmethod
    def _unresolved(case: BenchmarkCase, reason: str, resolved_at: str) -> dict[str, Any]:
        clean = case.model_copy(
            update={
                "immutable_revision": None,
                "license_spdx": None,
                "image": None,
                "provenance_state": "unresolved",
                "resolver_requirements": [reason.split(":", 1)[0]],
                "expected_contents": {},
                "assertions": [],
            }
        )
        return {
            "case": clean.model_dump(mode="json"),
            "case_id": case.id,
            "status": "unresolved",
            "reasons": [{"code": reason.split(":", 1)[0], "message": reason}],
            "provenance": {},
            "resolved_at": resolved_at,
        }
