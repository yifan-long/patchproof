"""Versioned evaluation contracts: BenchmarkCase, one-shot edits and fault specs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ..task.models import StrictModel


class OneShotEdit(StrictModel):
    """Bounded one-shot edit; ``old_text`` selects compact replacement mode."""

    path: str = Field(min_length=1, max_length=500)
    old_text: str | None = Field(default=None, max_length=200_000)
    new_text: str = Field(max_length=200_000)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("expected_sha256")
    @classmethod
    def validate_expected_hash(cls, value: str | None) -> str | None:
        if value is not None and any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("expected_sha256 must be a hexadecimal SHA-256")
        return value.lower() if value else value

    @field_validator("old_text")
    @classmethod
    def reject_empty_old_text(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("compact one-shot old_text must be non-empty")
        return value

    @property
    def mode(self) -> Literal["compact_replacement", "full_file"]:
        return "compact_replacement" if self.old_text is not None else "full_file"


class OneShotResponse(StrictModel):
    summary: str = Field(default="", max_length=2000)
    edits: list[OneShotEdit] = Field(default_factory=list, max_length=20)


class BenchmarkResourceLimits(StrictModel):
    """Hard resource caps shared by local and Docker evaluation backends."""

    cpu: float = Field(default=1.0, gt=0, le=64)
    memory_mb: int = Field(default=1024, gt=0, le=262_144)
    pids: int = Field(default=128, gt=0, le=32_768)
    output_bytes: int = Field(default=1_000_000, gt=0, le=100_000_000)


class FaultSpec(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    hook: str = Field(min_length=1, max_length=200)
    expected_status: str = Field(min_length=1, max_length=100)
    expected_failure: str | None = Field(default=None, max_length=100)
    expected_evidence: dict[str, Any] = Field(default_factory=dict)


class BenchmarkCase(StrictModel):
    """Versioned, self-contained evaluation case contract (v2)."""

    schema_version: Literal["patchproof.case.v2"] = "patchproof.case.v2"
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    suite: str = Field(min_length=1, max_length=100)
    source_kind: Literal["local", "git", "bugsinpy", "swebench"]
    repo_url: str | None = None
    local_path: str | None = None
    project: str | None = Field(default=None, max_length=200)
    bug_id: int | None = Field(default=None, ge=1)
    immutable_revision: str | None = None
    license_spdx: str | None = None
    source_url: str | None = None
    issue: str = Field(min_length=1, max_length=4000)
    goal: str = Field(min_length=1, max_length=4000)
    python_version: str | None = Field(default=None, pattern=r"^\d+\.\d+(?:\.\d+)?$")
    test_file: str | None = Field(default=None, max_length=500)
    executable_state: Literal[
        "not_applicable",
        "unverified",
        "verified_failing",
        "already_passing",
        "environment_unreproducible",
    ] = "not_applicable"
    setup_argv: list[list[str]] = Field(default_factory=list)
    required_check_argv: list[str] = Field(min_length=1, max_length=64)
    image: str | None = None
    allowed_edit_paths: list[str] = Field(default_factory=list, max_length=512)
    expected_changed_files: list[str] = Field(default_factory=list, max_length=512)
    timeout: int = Field(default=120, ge=1, le=3600)
    resources: BenchmarkResourceLimits = Field(default_factory=BenchmarkResourceLimits)
    repeats: int = Field(default=1, ge=1, le=100)
    privacy_public_code: bool = False
    provenance_state: Literal["resolved", "unresolved"] = "resolved"
    resolver_requirements: list[str] = Field(default_factory=list, max_length=32)
    fault: FaultSpec | None = None
    tags: list[str] = Field(default_factory=list, max_length=100)
    # Oracle data is limited to PatchProof-owned fixtures and never used by a
    # public/real model path.
    expected_contents: dict[str, str] = Field(default_factory=dict, max_length=32)
    assertions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_timeout_name(cls, value: Any) -> Any:
        """Accept the v0.2 spelling while serializing the v2 ``timeout`` field."""

        if not isinstance(value, dict) or "timeout_seconds" not in value:
            return value
        normalized = dict(value)
        legacy = normalized.pop("timeout_seconds")
        if "timeout" in normalized and normalized["timeout"] != legacy:
            raise ValueError("timeout and timeout_seconds must agree")
        normalized.setdefault("timeout", legacy)
        return normalized

    @field_validator("setup_argv", mode="before")
    @classmethod
    def normalize_setup_argv(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return [value] if value else []
        return value

    @field_validator("required_check_argv", "setup_argv", mode="after")
    @classmethod
    def validate_argv(cls, value: Any) -> Any:
        groups = value if value and isinstance(value[0], list) else [value]
        for argv in groups:
            if not argv:
                if value is not groups or value == []:
                    continue
                raise ValueError("argv must not be empty")
            for argument in argv:
                if not isinstance(argument, str) or not argument or "\x00" in argument:
                    raise ValueError("argv elements must be non-empty strings without NUL")
                if any(token in argument for token in ("\r", "\n", "&&", "||")):
                    raise ValueError("argv must not contain shell composition")
            executable = Path(argv[0]).name.lower()
            if executable in {"sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"}:
                raise ValueError("argv must not invoke a shell")
        return value

    @field_validator("allowed_edit_paths", "expected_changed_files", mode="after")
    @classmethod
    def validate_relative_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            normalized = path.replace("\\", "/")
            parts = normalized.split("/")
            if not normalized or normalized.startswith("/") or ":" in parts[0] or ".." in parts:
                raise ValueError(f"path must be repository-relative: {path}")
            if any(part in {"", "."} for part in parts):
                raise ValueError(f"path must be normalized: {path}")
        return value

    @field_validator("test_file")
    @classmethod
    def validate_test_file(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if normalized.startswith("/") or ":" in parts[0] or ".." in parts or any(part in {"", "."} for part in parts):
            raise ValueError("test_file must be a normalized repository-relative path")
        return normalized

    @field_validator("expected_contents", mode="after")
    @classmethod
    def validate_oracle_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for path, content in value.items():
            normalized = path.replace("\\", "/")
            parts = normalized.split("/")
            if not normalized or normalized.startswith("/") or ":" in parts[0] or ".." in parts:
                raise ValueError(f"oracle path must be repository-relative: {path}")
            if any(part in {"", "."} for part in parts):
                raise ValueError(f"oracle path must be normalized: {path}")
            if not isinstance(content, str):
                raise ValueError("oracle contents must be UTF-8 text")
        return value

    @field_validator("local_path")
    @classmethod
    def validate_local_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if not normalized or normalized.startswith("/") or ":" in parts[0] or ".." in parts:
            raise ValueError("local_path must be a PatchProof-relative path")
        if any(part in {"", "."} for part in parts):
            raise ValueError("local_path must be normalized")
        return value

    @field_validator("source_url", "repo_url")
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        if value is not None and not (
            value.startswith("https://") or value.startswith("http://") or value.startswith("local://")
        ):
            raise ValueError("source/repository URL must use http(s) or local scheme")
        return value

    @model_validator(mode="after")
    def validate_source_and_provenance(self) -> BenchmarkCase:
        public_kind = self.source_kind in {"bugsinpy", "swebench"}
        if public_kind != self.privacy_public_code:
            raise (
                ValueError("public dataset cases must set privacy_public_code=true")
                if public_kind
                else ValueError("local/git cases must set privacy_public_code=false")
            )
        if self.source_kind == "local":
            if not self.local_path:
                raise ValueError("local cases require local_path")
            if not self.image or not _is_pinned_image(self.image):
                raise ValueError("local cases require an explicit immutable image marker or digest")
            if self.repo_url or self.project or self.bug_id is not None:
                raise ValueError("local cases cannot include public repository identifiers")
            if self.provenance_state != "resolved":
                raise ValueError("local cases must have resolved provenance")
            if self.executable_state not in {"not_applicable", "verified_failing"}:
                raise ValueError("local cases cannot claim a public runtime resolution state")
        elif self.source_kind == "git":
            if not self.repo_url or not self.immutable_revision:
                raise ValueError("git cases require repo_url and immutable_revision")
            if not self.image or not _is_pinned_image(self.image):
                raise ValueError("git cases require an image pinned by digest")
            if self.project or self.bug_id is not None:
                raise ValueError("git cases do not use dataset project/bug identifiers")
        else:
            if not self.project or self.bug_id is None or not self.source_url:
                raise ValueError("public cases require project, bug_id and official source_url")
            if not self.source_url.startswith("https://") or (
                self.repo_url is not None and not self.repo_url.startswith("https://")
            ):
                raise ValueError("public provenance URLs must use HTTPS")
            if self.provenance_state == "unresolved":
                if not self.resolver_requirements:
                    raise ValueError("unresolved public cases require resolver_requirements")
                if any(value is not None for value in (self.immutable_revision, self.license_spdx, self.image)):
                    raise ValueError("unresolved public cases must not claim revision, license or image metadata")
            else:
                if not self.immutable_revision or not _is_immutable_revision(self.immutable_revision):
                    raise ValueError("resolved public cases require a full immutable commit")
                if not self.license_spdx or _is_unknown_license(self.license_spdx):
                    raise ValueError("resolved public cases require a known SPDX license")
                if not self.image or self.image.startswith("local://") or not _is_pinned_image(self.image):
                    raise ValueError("resolved public cases require an image pinned by digest")
        if self.immutable_revision and not _is_immutable_revision(self.immutable_revision):
            raise ValueError("revision must be an immutable commit hash, not a branch/tag/latest ref")
        if self.image and ":latest" in self.image.lower():
            raise ValueError("floating :latest image is not allowed")
        if self.fault and self.fault.id != self.id and self.suite == "fault-injection":
            raise ValueError("fault case id and fault spec id must match")
        if self.allowed_edit_paths and any(
            path not in set(self.allowed_edit_paths) for path in self.expected_changed_files
        ):
            raise ValueError("expected_changed_files must be within allowed_edit_paths")
        if self.expected_contents:
            if self.source_kind != "local" or self.privacy_public_code:
                raise ValueError("expected_contents oracle is restricted to PatchProof-owned local cases")
            if set(self.expected_contents) != set(self.expected_changed_files):
                raise ValueError("expected_contents paths must exactly match expected_changed_files")
        if self.assertions and self.source_kind != "local":
            raise ValueError("oracle assertions are restricted to PatchProof-owned local cases")
        if self.suite == "mini-repos" and (
            len(self.expected_contents) != 1 or len(self.expected_changed_files) != 1
        ):
            raise ValueError("mini-repo smoke cases require exactly one deterministic oracle edit")
        return self

    @property
    def fixture(self) -> str:
        """Compatibility view for the v0.2 deterministic benchmark loader."""

        return self.local_path or ""

    @property
    def check_command(self) -> str:
        import shlex

        return shlex.join(self.required_check_argv)

    @property
    def repo(self) -> str | None:
        return self.repo_url

    @property
    def expected_files(self) -> list[str]:
        return self.expected_changed_files

    @property
    def timeout_seconds(self) -> int:
        """Compatibility view for v0.2 callers; v2 manifests use ``timeout``."""

        return self.timeout

    def without_oracle(self) -> BenchmarkCase:
        """Return the runtime view used by real/public evaluation paths."""

        return self.model_copy(update={"expected_contents": {}, "assertions": []})


def _is_immutable_revision(value: str) -> bool:
    return len(value) in {40, 64} and all(char in "0123456789abcdefABCDEF" for char in value)


def _is_unknown_license(value: str) -> bool:
    known = {
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "MIT",
        "MPL-2.0",
        "PSF-2.0",
        "Unlicense",
    }
    return value.strip() not in known


def _is_pinned_image(value: str) -> bool:
    if value.startswith("local://"):
        return True
    digest = value.removeprefix("sha256:") if value.startswith("sha256:") else value.rsplit("@sha256:", 1)[-1]
    has_marker = value.startswith("sha256:") or "@sha256:" in value
    return has_marker and len(digest) == 64 and all(char in "0123456789abcdefABCDEF" for char in digest)
