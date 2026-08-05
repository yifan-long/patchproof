"""Patch Receipt: a content-addressed, verifiable completion claim."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def receipt_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    payload = dict(receipt)
    payload.pop("receipt_hash", None)
    # The artifact hash is a detached self-check over the canonical sealed
    # receipt without this field. Including the hash in its own input would
    # require solving a fixed point and would make the artifact unverifiable.
    payload.pop("artifact_sha256", None)
    return payload


def compute_receipt_hash(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(receipt_payload(receipt)).encode("utf-8")).hexdigest()


def seal_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(receipt)
    sealed.pop("artifact_sha256", None)
    sealed["receipt_hash"] = compute_receipt_hash(sealed)
    sealed["artifact_sha256"] = _artifact_material_hash(sealed)
    return sealed


def verify_receipt(receipt: dict[str, Any], expected_hash: str | None = None) -> bool:
    recorded = receipt.get("receipt_hash")
    if not isinstance(recorded, str):
        return False
    if expected_hash is not None and recorded != expected_hash:
        return False
    if recorded != compute_receipt_hash(receipt):
        return False
    artifact_hash = receipt.get("artifact_sha256")
    return artifact_hash is None or artifact_hash == _artifact_material_hash(receipt)


def _artifact_material_hash(receipt: dict[str, Any]) -> str:
    material = dict(receipt)
    material.pop("artifact_sha256", None)
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def receipt_path(task_id: str, root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return base / "data" / "runs" / task_id / "receipt.json"


def write_receipt_atomic(
    task_id: str,
    receipt: dict[str, Any],
    *,
    root: str | Path | None = None,
) -> tuple[Path, str]:
    """Write the canonical sealed receipt and return path plus file hash."""

    if not verify_receipt(receipt):
        raise ValueError("只能写入通过逻辑 hash 校验的 sealed receipt")
    target = receipt_path(task_id, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(receipt).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Windows may not allow opening a directory handle; the atomic
            # replace above is still the required durability boundary there.
            pass
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target, hashlib.sha256(payload).hexdigest()


def verify_receipt_file(path: str | Path, expected_file_hash: str | None = None) -> bool:
    """Verify canonical bytes, logical receipt hash, and optional file hash."""

    target = Path(path)
    try:
        raw = target.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or canonical_json(parsed).encode("utf-8") != raw:
            return False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if expected_file_hash is not None and hashlib.sha256(raw).hexdigest() != expected_file_hash:
        return False
    return verify_receipt(parsed)


def build_patch_receipt(
    *,
    task_id: str,
    goal: str,
    workspace: dict[str, Any],
    model: dict[str, Any],
    plan: dict[str, Any] | None,
    tool_stats: dict[str, Any],
    changed_files: list[dict[str, Any]],
    diff_hash: str,
    commands: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    tests: dict[str, Any] | None,
    event_chain_head: str,
    started_at: str,
    ended_at: str | None,
    verdict: str,
) -> dict[str, Any]:
    return seal_receipt(
        {
            "schema_version": "patchproof.receipt.v1",
            "task": {"id": task_id, "goal": goal},
            "workspace": workspace,
            "model": model,
            "plan_summary": {
                "summary": (plan or {}).get("summary", ""),
                "steps": (plan or {}).get("steps", []),
                "checks": (plan or {}).get("checks", []),
            },
            "tool_stats": tool_stats,
            "changed_files": changed_files,
            "diff_hash": diff_hash,
            "commands": commands,
            "approvals": approvals,
            "tests": tests or {},
            "event_chain_head": event_chain_head,
            "started_at": started_at,
            "ended_at": ended_at or datetime.now(UTC).isoformat(),
            "verdict": verdict,
        }
    )
