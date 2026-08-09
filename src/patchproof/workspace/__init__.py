"""Isolated workspaces, guarded write-back strategies and the oracle deny policy."""

from .artifact_policy import (
    DENIED_ARTIFACT_NAMES,
    copytree_without_oracles,
    is_denied_artifact,
    sanitize_check_output,
    tree_identity,
)
from .strategies import (
    COPY_IGNORE,
    MANIFEST_EXCLUDED_DIRS,
    PROTECTED_FILES,
    GitWorktreeWorkspace,
    SnapshotWorkspace,
    Workspace,
    WorkspaceBoundaryError,
    WorkspacePreconditionError,
    WorkspaceProtocol,
    WorkspaceStrategy,
    open_workspace,
    select_workspace,
    sha256_bytes,
    sha256_text,
)

__all__ = [
    "COPY_IGNORE",
    "DENIED_ARTIFACT_NAMES",
    "MANIFEST_EXCLUDED_DIRS",
    "PROTECTED_FILES",
    "GitWorktreeWorkspace",
    "SnapshotWorkspace",
    "Workspace",
    "WorkspaceBoundaryError",
    "WorkspacePreconditionError",
    "WorkspaceProtocol",
    "WorkspaceStrategy",
    "copytree_without_oracles",
    "is_denied_artifact",
    "open_workspace",
    "sanitize_check_output",
    "select_workspace",
    "sha256_bytes",
    "sha256_text",
    "tree_identity",
]
