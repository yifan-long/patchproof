from collections.abc import Sequence


def page[T](items: Sequence[T], *, offset: int, limit: int) -> list[T]:
    if offset < 0 or limit < 1:
        raise ValueError("offset must be non-negative and limit must be positive")
    return list(items[offset : offset + limit])


def has_next[T](items: Sequence[T], *, offset: int, limit: int) -> bool:
    # Intentional fixture bug: equality means the current page is the last.
    return offset + limit <= len(items)
