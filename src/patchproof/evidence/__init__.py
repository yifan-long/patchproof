"""Small, shared primitives for durable evidence.

The public modules keep the historical import paths.  New code should use
these helpers instead of defining another JSON or hash format.
"""

from .canonical import canonical_json, hash_bytes, hash_json, hash_text

__all__ = ["canonical_json", "hash_bytes", "hash_json", "hash_text"]
