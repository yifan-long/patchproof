"""Infrastructure adapters with SQLite as the durable truth store."""

from .sqlite import SQLiteStore

__all__ = ["SQLiteStore"]
