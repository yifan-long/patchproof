"""Isolated workspaces and guarded write-back strategies."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

from .artifact_policy import is_denied_artifact

COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "data",
    ".env",
    ".env.*",
)

PROTECTED_FILES = {
    ".env",
    ".env.example",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "pipfile.lock",
}

MANIFEST_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "data",
}


class WorkspaceProtocol(Protocol):
    original: Path
    staging: Path
    kind: str
    reason: str
    baseline: dict[str, Any]

    def create(self) -> None: ...

    def open_existing(self) -> None: ...

    def read_file(self, relative_path: str, start_line: int = 1, end_line: int | None = None) -> str: ...

    def search_repo(self, query: str, max_results: int = 20) -> list[dict[str, Any]]: ...

    def current_sha256(self, relative_path: str) -> str: ...

    def apply_edit(
        self,
        relative_path: str,
        new_text: str,
        *,
        expected_sha256: str | None = None,
        old_text: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]: ...

    def diff(self) -> tuple[str, list[str]]: ...

    def apply(self) -> list[str]: ...

    def cleanup(self) -> None: ...


# Public names kept intentionally generic so a future Docker/VM strategy can
# implement the same contract without changing AgentRunner.
WorkspaceStrategy = WorkspaceProtocol
Workspace = WorkspaceProtocol


class WorkspacePreconditionError(RuntimeError):
    pass


class WorkspaceBoundaryError(ValueError):
    pass


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode("utf-8"))


def _relative(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path.replace("\\", "/"))
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise WorkspaceBoundaryError("编辑路径必须是工作区内的相对路径")
    if not candidate.parts or any(part in {"", "."} for part in candidate.parts):
        raise WorkspaceBoundaryError("编辑路径不能为空")
    resolved = (root / candidate).resolve()
    if root.resolve() not in resolved.parents:
        raise WorkspaceBoundaryError("编辑路径越过工作区边界")
    return resolved.relative_to(root.resolve())


def _is_protected(relative: Path) -> bool:
    return is_denied_artifact(relative) or relative.name.lower() in PROTECTED_FILES or any(
        part.startswith(".") and part not in {".github", ".well-known"} for part in relative.parts
    )


def _manifest(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in MANIFEST_EXCLUDED_DIRS for part in relative.parts) or _is_protected(relative):
            continue
        try:
            content = path.read_bytes()
            result[relative.as_posix()] = {"sha256": sha256_bytes(content), "size": len(content)}
        except OSError:
            continue
    return result


def _read_text(path: Path, max_file_bytes: int) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size > max_file_bytes:
        raise ValueError(f"文件超过读取大小限制: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 UTF-8 文本，不能作为 typed tool 上下文: {path}") from exc


class _WorkspaceTextMixin:
    max_file_bytes: int

    def _resolve_staging_path(self, relative_path: str) -> tuple[Path, Path]:
        relative = _relative(self.staging, relative_path)
        if _is_protected(relative):
            raise WorkspaceBoundaryError("禁止编辑敏感或隐藏配置文件")
        return relative, self.staging / relative

    def read_file(self, relative_path: str, start_line: int = 1, end_line: int | None = None) -> str:
        relative = _relative(self.staging, relative_path)
        if is_denied_artifact(relative):
            raise WorkspaceBoundaryError("禁止读取 benchmark answer/oracle artifact")
        content = _read_text(self.staging / relative, self.max_file_bytes)
        lines = content.splitlines(keepends=True)
        return "".join(lines[start_line - 1 : end_line])

    def current_sha256(self, relative_path: str) -> str:
        relative, target = self._resolve_staging_path(relative_path)
        content = target.read_bytes() if target.exists() else b""
        return sha256_bytes(content)

    def search_repo(self, query: str, max_results: int = 20) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        results: list[dict[str, Any]] = []
        needle = query.lower()
        for path in sorted(self.staging.rglob("*")):
            if len(results) >= max_results or not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.staging)
            if any(part in MANIFEST_EXCLUDED_DIRS for part in relative.parts) or is_denied_artifact(relative):
                continue
            try:
                content = _read_text(path, self.max_file_bytes)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if needle in line.lower() or needle in relative.as_posix().lower():
                    results.append({"path": relative.as_posix(), "line": line_number, "text": line[:500]})
                    if len(results) >= max_results:
                        break
        return results

    def _write_text(self, relative_path: str, content: str) -> dict[str, Any]:
        relative, target = self._resolve_staging_path(relative_path)
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ValueError(f"文件超过大小限制: {relative_path}")
        before = target.read_bytes() if target.exists() else b""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=target.parent, prefix=f".{target.name}.") as file:
                temporary = file.name
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return {
            "path": relative.as_posix(),
            "before_sha256": sha256_bytes(before),
            "after_sha256": sha256_bytes(encoded),
            "bytes": len(encoded),
        }

    def apply_edit(
        self,
        relative_path: str,
        new_text: str,
        *,
        expected_sha256: str | None = None,
        old_text: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        relative, target = self._resolve_staging_path(relative_path)
        current = target.read_bytes() if target.exists() else b""
        current_hash = sha256_bytes(current)
        if expected_sha256 is None and old_text is None:
            raise WorkspacePreconditionError("apply_edit 必须提供 expected_sha256 或 old_text 前置条件")
        if expected_sha256 is not None and current_hash != expected_sha256.lower():
            raise WorkspacePreconditionError(
                f"{relative.as_posix()} 前置 hash 不匹配: expected={expected_sha256} actual={current_hash}"
            )
        if old_text is not None:
            if old_text == "":
                raise WorkspacePreconditionError("old_text 不能为空")
            current_text = current.decode("utf-8", errors="strict")
            crlf = "\r\n" in current_text
            # Line-ending-agnostic matching: a Windows checkout may store CRLF
            # while the model emits LF from read_file observations.
            normalized_current = current_text.replace("\r\n", "\n").replace("\r", "\n")
            normalized_old = old_text.replace("\r\n", "\n").replace("\r", "\n")
            matches = normalized_current.count(normalized_old)
            if matches == 0:
                raise WorkspacePreconditionError(f"{relative.as_posix()} 前置文本不存在，拒绝盲写")
            if matches != 1:
                raise WorkspacePreconditionError(f"{relative.as_posix()} 前置文本不唯一，拒绝模糊替换")
            normalized_new = new_text.replace("\r\n", "\n").replace("\r", "\n")
            next_text = normalized_current.replace(normalized_old, normalized_new, 1)
            if crlf:
                next_text = next_text.replace("\n", "\r\n")
        else:
            next_text = new_text
        result = self._write_text(relative.as_posix(), next_text)
        result["reason"] = reason
        return result

    def safe_write(self, relative_path: str, content: str) -> Path:
        """Compatibility helper for the v0.1 tests; typed tools use apply_edit."""
        relative, _ = self._resolve_staging_path(relative_path)
        self._write_text(relative.as_posix(), content)
        return self.staging / relative

    def diff(self) -> tuple[str, list[str]]:
        before_manifest = _manifest(self.original)
        after_manifest = _manifest(self.staging)
        changed = sorted(set(before_manifest) | set(after_manifest))
        changed = [
            relative
            for relative in changed
            if before_manifest.get(relative) != after_manifest.get(relative)
            and not _is_protected(Path(relative))
        ]
        chunks: list[str] = []
        for relative in changed:
            before_path = self.original / relative
            after_path = self.staging / relative
            try:
                before = _read_text(before_path, self.max_file_bytes) if before_path.exists() else ""
                after = _read_text(after_path, self.max_file_bytes) if after_path.exists() else ""
            except (OSError, UnicodeDecodeError, ValueError):
                chunks.append(f"Binary files a/{relative} and b/{relative} differ\n")
                continue
            chunks.extend(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        return "".join(chunks), changed

    def change_records(self) -> list[dict[str, Any]]:
        before = _manifest(self.original)
        after = _manifest(self.staging)
        records = []
        for relative in sorted(set(before) | set(after)):
            if before.get(relative) == after.get(relative) or _is_protected(Path(relative)):
                continue
            records.append(
                {
                    "path": relative,
                    "before_sha256": before.get(relative, {}).get("sha256", sha256_bytes(b"")),
                    "after_sha256": after.get(relative, {}).get("sha256", sha256_bytes(b"")),
                    "before_size": before.get(relative, {}).get("size", 0),
                    "after_size": after.get(relative, {}).get("size", 0),
                }
            )
        return records

    def _atomic_writeback(self, changed: list[str]) -> None:
        if any(not (self.staging / relative).exists() for relative in changed):
            missing = next(relative for relative in changed if not (self.staging / relative).exists())
            raise RuntimeError(f"删除文件的变更不支持自动应用，请人工处理: {missing}")
        backups: dict[Path, bytes | None] = {}
        try:
            for relative in changed:
                target = self.original / relative
                backups[target] = target.read_bytes() if target.exists() else None
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        "wb",
                        delete=False,
                        dir=target.parent,
                        prefix=f".{target.name}.",
                    ) as file:
                        temporary = file.name
                        file.write((self.staging / relative).read_bytes())
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(temporary, target)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
        except Exception:
            for target, content in backups.items():
                if content is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.write_bytes(content)
            raise


class SnapshotWorkspace(_WorkspaceTextMixin):
    """Copy-based isolation for non-Git or dirty repositories."""

    kind = "snapshot"

    def __init__(self, original: Path, staging: Path, max_file_bytes: int = 200_000, reason: str | None = None):
        self.original = original.resolve()
        self.staging = staging.resolve()
        self.max_file_bytes = max_file_bytes
        self.reason = reason or "非 Git 仓库或源仓库存在未提交变更，使用 snapshot fallback"
        self.manifest_path = self.staging.parent / "source-manifest.json"
        self.baseline: dict[str, Any] = {}

    def create(self) -> None:
        self.staging.parent.mkdir(parents=True, exist_ok=True)
        if self.staging.exists():
            shutil.rmtree(self.staging)
        baseline_manifest = _manifest(self.original)
        shutil.copytree(self.original, self.staging, ignore=COPY_IGNORE)
        self.baseline = {"kind": self.kind, "source_manifest": baseline_manifest, "status": "fallback"}
        self.manifest_path.write_text(json.dumps(self.baseline, ensure_ascii=False, indent=2), encoding="utf-8")

    def open_existing(self) -> None:
        if not self.manifest_path.is_file() or not self.staging.is_dir():
            raise RuntimeError("缺少 snapshot 工作区或源仓库 manifest，拒绝继续")
        self.baseline = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _assert_original_unchanged(self) -> None:
        if not self.baseline:
            self.open_existing()
        if self.baseline.get("source_manifest") != _manifest(self.original):
            raise RuntimeError("真实仓库在任务期间发生变化，拒绝覆盖；请重新运行任务")

    def apply(self) -> list[str]:
        _, changed = self.diff()
        if not changed:
            return []
        self._assert_original_unchanged()
        self._atomic_writeback(changed)
        return changed

    def cleanup(self) -> None:
        # Cleanup is explicit and normally deferred so the receipt can point
        # to a replayable workspace after the task finishes.
        return None

    @property
    def metadata(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "baseline": self.baseline}


class GitWorktreeWorkspace(_WorkspaceTextMixin):
    """Detached Git worktree for clean repositories."""

    kind = "git_worktree"

    def __init__(self, original: Path, staging: Path, max_file_bytes: int = 200_000):
        self.original = original.resolve()
        self.staging = staging.resolve()
        self.max_file_bytes = max_file_bytes
        self.reason = "源仓库为干净 Git worktree，使用 detached worktree 隔离"
        self.metadata_path = self.staging.parent / "workspace.json"
        self.baseline: dict[str, Any] = {}

    @staticmethod
    def inspect(original: Path) -> tuple[bool, str, str | None]:
        try:
            top = subprocess.run(
                ["git", "-C", str(original), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                check=False,
            )
            if top.returncode != 0:
                return False, "目标目录不是 Git 仓库，使用 snapshot fallback", None
            top_path = Path(top.stdout.strip()).resolve()
            if top_path != original.resolve():
                return False, "目标路径是 Git 子目录，使用 snapshot fallback 保证边界清晰", None
            status = subprocess.run(
                ["git", "-C", str(original), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                check=False,
            )
            head = subprocess.run(
                ["git", "-C", str(original), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                check=False,
            )
            if status.returncode != 0 or head.returncode != 0:
                return False, "Git 状态不可读，使用 snapshot fallback", None
            if status.stdout.strip():
                return False, "Git 仓库存在 dirty/untracked 变更，使用 snapshot fallback", head.stdout.strip()
            return True, "源仓库干净，使用 Git worktree 隔离", head.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False, "Git 不可用，使用 snapshot fallback", None

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=self.original,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"Git 命令失败: git {' '.join(args)}\n{result.stderr.strip()}")
        return result

    def create(self) -> None:
        self.staging.parent.mkdir(parents=True, exist_ok=True)
        if self.staging.exists():
            shutil.rmtree(self.staging)
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("worktree", "add", "--detach", str(self.staging), head)
        self.baseline = {
            "kind": self.kind,
            "head": head,
            "status": self._git("status", "--porcelain", "--untracked-files=all").stdout,
            "source_manifest": _manifest(self.original),
        }
        self.metadata_path.write_text(json.dumps(self.baseline, ensure_ascii=False, indent=2), encoding="utf-8")

    def open_existing(self) -> None:
        if not self.metadata_path.is_file() or not self.staging.is_dir():
            raise RuntimeError("缺少 Git worktree 或 workspace metadata，拒绝继续")
        self.baseline = json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def _assert_original_unchanged(self) -> None:
        if not self.baseline:
            self.open_existing()
        head = self._git("rev-parse", "HEAD").stdout.strip()
        status = self._git("status", "--porcelain", "--untracked-files=all").stdout
        if head != self.baseline.get("head") or status != self.baseline.get("status"):
            raise RuntimeError("Git 源仓库 HEAD 或工作树已变化，拒绝覆盖；请重新运行任务")
        if self.baseline.get("source_manifest") != _manifest(self.original):
            raise RuntimeError("源仓库文件 manifest 已变化，拒绝覆盖；请重新运行任务")

    def apply(self) -> list[str]:
        _, changed = self.diff()
        if not changed:
            self._assert_original_unchanged()
            return []
        self._assert_original_unchanged()
        self._atomic_writeback(changed)
        return changed

    def cleanup(self) -> None:
        if not self.staging.exists():
            return
        self._git("worktree", "remove", "--force", str(self.staging), check=False)

    @property
    def metadata(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "baseline": self.baseline}


def select_workspace(
    original: Path,
    staging: Path,
    max_file_bytes: int = 200_000,
    *,
    allow_git_worktree: bool = True,
) -> WorkspaceProtocol:
    if allow_git_worktree:
        eligible, reason, _ = GitWorktreeWorkspace.inspect(original)
        if eligible:
            return GitWorktreeWorkspace(original, staging, max_file_bytes)
        return SnapshotWorkspace(original, staging, max_file_bytes, reason=reason)
    return SnapshotWorkspace(original, staging, max_file_bytes, reason="配置关闭 Git worktree，使用 snapshot fallback")


def open_workspace(
    original: Path,
    staging: Path,
    kind: str,
    max_file_bytes: int = 200_000,
) -> WorkspaceProtocol:
    if kind == GitWorktreeWorkspace.kind:
        return GitWorktreeWorkspace(original, staging, max_file_bytes)
    if kind == SnapshotWorkspace.kind:
        return SnapshotWorkspace(original, staging, max_file_bytes)
    raise ValueError(f"未知 workspace 类型: {kind}")
