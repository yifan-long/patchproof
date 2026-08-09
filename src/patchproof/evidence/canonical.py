"""规范化序列化与 SHA-256 —— 所有证据哈希的唯一入口。

做什么
------
给全系统提供"同一份数据在任何环境序列化结果都逐字节一致"的 JSON，以及基于它的哈希。

怎么实现
--------
canonical_json 固定 sort_keys=True + 无空格分隔符 + ensure_ascii=False；
hash_bytes / hash_text / hash_json 逐层包装。

为什么
------
事件链、Receipt、报告 ID 都以哈希互指，若序列化不稳定（键顺序/空格/转义随环境变化），
同一个对象会算出不同哈希，证据就全部不可复现。这里就是那个"唯一真相格式"。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically for hashes, SQLite and JSONL."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_text(value: str) -> str:
    return hash_bytes(value.encode("utf-8"))


def hash_json(value: Any) -> str:
    return hash_text(canonical_json(value))
