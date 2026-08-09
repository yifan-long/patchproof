"""Central deny policy for benchmark answer/oracle artifacts."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

DENIED_ARTIFACT_NAMES = frozenset(
    {
        "bug_patch.txt",
        "bug.patch",
        "fix.patch",
        "fixed.patch",
        "answer.patch",
        "oracle.patch",
        "patch.diff",
        "fix.diff",
        "answer.diff",
        "oracle.diff",
    }
)
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*=\s*\S+")
_TEST_DURATION = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")
_ISO_TIMESTAMP = re.compile(r"(?<![\d:])\d{1,2}:\d{2}:\d{2}(?:\.\d+)?")


def is_denied_artifact(path: str | Path) -> bool:
    normalized = str(path).replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].lower()
    return (
        name in DENIED_ARTIFACT_NAMES
        or name.startswith("bug_patch.")
        or name.startswith("fixed_patch.")
        or name.startswith("answer_patch.")
        or name.startswith("oracle_patch.")
    )


def copytree_without_oracles(source: Path, destination: Path) -> None:
    def ignored(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if is_denied_artifact(name)}

    shutil.copytree(source, destination, ignore=ignored)


def sanitize_check_output(value: str, *, workspace: Path, limit: int = 4000) -> str:
    """Bound execution output without exposing host roots, secrets, or oracle paths."""

    normalized = value.replace(str(workspace), "<workspace>").replace(str(workspace).replace("\\", "/"), "<workspace>")
    safe_lines: list[str] = []
    for line in normalized.splitlines():
        if any(name in line.lower() for name in DENIED_ARTIFACT_NAMES) or is_denied_artifact(line.strip()):
            safe_lines.append("<oracle-artifact-output-redacted>")
            continue
        safe_lines.append(_SECRET_ASSIGNMENT.sub("<secret-redacted>", line))
    safe = _TEST_DURATION.sub("in <duration>", "\n".join(safe_lines))
    safe = _ISO_TIMESTAMP.sub("<time>", safe)
    return safe[-limit:]


def tree_identity(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        relative = path.relative_to(root)
        if ".git" in relative.parts or is_denied_artifact(relative):
            continue
        name = relative.as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
