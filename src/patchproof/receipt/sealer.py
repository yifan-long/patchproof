"""Patch Receipt —— 内容寻址、可自校验的"完成证据"。

做什么
------
把任务的关键事实（计划摘要、工具统计、文件前后哈希、diff 哈希、命令、审批、测试证据、
事件链头）密封成一份不可抵赖的 Receipt：任何字段被改，逻辑哈希或文件字节哈希都会对不上。

怎么实现
--------
- seal_receipt：先剔除 self-reference 字段再算 ``receipt_hash``，再补 ``artifact_sha256``。
- write_receipt_atomic：临时文件 + ``os.replace`` + fsync，原子落盘。
- verify_receipt / verify_receipt_file：分别校验"逻辑哈希"与"文件字节+规范序列化"。

为什么
------
- 哈希不能包含自己：若 receipt_hash 参与自身输入会变成不动点，无法验证，所以先从 payload 剔除。
- 文件字节哈希单独存 SQLite：API 能区分"Receipt 逻辑完好"与"文件被改过/丢了"两个不同的失败。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import PATCHPROOF_ROOT
from ..evidence.canonical import canonical_json, hash_bytes, hash_json


def receipt_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    payload = dict(receipt)
    payload.pop("receipt_hash", None)
    # The artifact hash is a detached self-check over the canonical sealed
    # receipt without this field. Including the hash in its own input would
    # require solving a fixed point and would make the artifact unverifiable.
    payload.pop("artifact_sha256", None)
    return payload


def compute_receipt_hash(receipt: dict[str, Any]) -> str:
    return hash_json(receipt_payload(receipt))


def seal_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(receipt)
    # 计算前先剔除自指字段：artifact_sha256 是对"没有它自己"的密封结果的离体自校验。
    # 若让它参与自身输入，就变成求不动点，永远无法验证。
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
    return hash_json(material)


def receipt_path(task_id: str, root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else PATCHPROOF_ROOT
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
    return target, hash_bytes(payload)


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
    if expected_file_hash is not None and hash_bytes(raw) != expected_file_hash:
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
