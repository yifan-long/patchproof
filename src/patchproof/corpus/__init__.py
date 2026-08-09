"""Corpus loading and explicit, content-addressed source planning."""

from .loader import (
    CacheManager,
    CommandResult,
    CorpusResolver,
    FetchPlan,
    FetchPlanner,
    SubprocessCommandRunner,
    build_fetch_plan,
    canonical_case_payload,
    content_addressed_cache_key,
    execute_fetch_plan,
    load_cases,
)

__all__ = [
    "CacheManager",
    "CommandResult",
    "CorpusResolver",
    "FetchPlan",
    "FetchPlanner",
    "SubprocessCommandRunner",
    "build_fetch_plan",
    "canonical_case_payload",
    "content_addressed_cache_key",
    "execute_fetch_plan",
    "load_cases",
]
