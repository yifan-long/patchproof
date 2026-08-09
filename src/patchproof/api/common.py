"""Shared helpers for the FastAPI routers: project-root path guards, corpus
loading, and the evaluation trigger request schema."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import PATCHPROOF_ROOT
from ..corpus import load_cases

_APP_ROOT = PATCHPROOF_ROOT


def _project_path(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = _APP_ROOT / candidate
    return candidate.resolve()


def _project_data_path(value: str) -> Path:
    candidate = _project_path(value)
    root = (_APP_ROOT / "data").resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("evaluation report path must stay under patchproof/data")
    return candidate


def _project_manifest_path(value: str) -> Path:
    """Resolve evaluation manifests without allowing arbitrary file reads."""

    candidate = _project_path(value)
    if candidate != _APP_ROOT and _APP_ROOT not in candidate.parents:
        raise ValueError("evaluation manifest must stay under patchproof")
    return candidate


def _corpus_cases(*, include_public: bool = True) -> list[Any]:
    cases = load_cases(_APP_ROOT / "benchmarks" / "manifest.v2.json")
    if include_public:
        cases.extend(load_cases(_APP_ROOT / "benchmarks" / "public" / "bugs-in-py.v2.json"))
    return cases


class EvaluationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    manifest: str = "benchmarks/manifest.v2.json"
    include_public: bool = True
    confirm_real: bool = False
    confirm_public_code_egress: bool = False
    confirm_download: bool = False
    budget_stage: Literal["first-pass", "expansion"] = "first-pass"
    repeats: int = Field(default=1, ge=1, le=100)
    max_cases: int = Field(default=5, ge=1, le=100)
    max_requests: int = Field(default=40, ge=1, le=10000)
    max_tokens: int = Field(default=32768, ge=1, le=10_000_000)
    max_cost_usd: float = Field(default=2.0, gt=0, le=1000)
    jsonl_path: str = "data/evaluation-runs.jsonl"
