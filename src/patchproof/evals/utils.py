"""Pure helpers for the benchmark surface: manifest loading, metrics
aggregation, cost estimation, oracle edits, atomic JSON output, and the
real-evaluation failure envelope.

These functions carry no runner/executor state and are kept separate from
``benchmark.py`` so the CLI + harness file stays focused on orchestration.
``benchmark.py`` re-exports the public names so existing imports keep working.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..llm.budget import BudgetLedger, BudgetLimits
from ..policy.commands import parse_command
from .models import BenchmarkCase

ModelFactory = Callable[[str, BenchmarkCase], Any]
MAX_BASELINE_EDIT_PAYLOAD_BYTES = 200_000


def _write_json_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=output.name + ".",
        suffix=".tmp",
        dir=output.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(output)


def _empty_ledger_snapshot(
    *,
    max_requests: int,
    max_tokens: int,
    max_cost_usd: float,
    reserve_output_tokens: int,
    cost_per_million_tokens: float,
) -> dict[str, Any]:
    return BudgetLedger(
        BudgetLimits(
            max_requests=max_requests,
            max_input_tokens=max_tokens,
            max_output_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
            reserve_output_tokens=reserve_output_tokens,
            cost_per_million_tokens=cost_per_million_tokens,
        )
    ).snapshot()


def _real_failure_envelope(
    *,
    category: str,
    message: str,
    provider: dict[str, Any],
    budget_stage: str,
    max_requests: int,
    max_tokens: int,
    max_cost_usd: float,
    reserve_output_tokens: int,
    selected_case_ids: list[str],
    repeats: int,
    ledger: dict[str, Any] | None = None,
    complete_pairs: int = 0,
    partial_records: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version": "patchproof.real-evaluation-failure.v1",
        "patchproof_version": __version__,
        "success": False,
        "failure_category": category,
        "message": message,
        "timestamp": datetime.now(UTC).isoformat(),
        "provider": provider,
        "budget": {
            "stage": budget_stage,
            "limits": {
                "max_requests": max_requests,
                "max_input_tokens": max_tokens,
                "max_output_tokens": max_tokens,
                "max_cost_usd": max_cost_usd,
                "reserve_output_tokens": reserve_output_tokens,
            },
            "ledger": ledger,
        },
        "selection": {"case_ids": selected_case_ids, "repeats": repeats},
        "comparison": {
            "status": "not_produced",
            "head_to_head_eligible": False,
            "incomplete_pairs_excluded": True,
            "complete_pairs_before_failure": complete_pairs,
            "partial_records_retained_in_jsonl": partial_records,
        },
    }


def load_cases(manifest: str | Path) -> list[BenchmarkCase]:
    path = Path(manifest)
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("cases", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("benchmark manifest 必须是 cases 数组或 {cases: [...]} 对象")
    cases: list[BenchmarkCase] = []
    for item in items:
        if item.get("schema_version") == "patchproof.case.v2":
            cases.append(BenchmarkCase.model_validate(item))
            continue
        # Read the v0.2 deterministic smoke manifest without weakening the
        # v2 model: legacy data is upgraded into a local, PatchProof-owned case.
        check_argv = parse_command(item["check_command"]).argv
        cases.append(
            BenchmarkCase.model_validate(
                {
                    "schema_version": "patchproof.case.v2",
                    "id": item["id"],
                    "suite": "legacy-smoke",
                    "source_kind": "local",
                    "local_path": item.get("fixture", item.get("repo", "")),
                    "issue": item["goal"],
                    "goal": item["goal"],
                    "required_check_argv": check_argv,
                    "image": "local://patchproof-python312",
                    "expected_changed_files": item.get("expected_files", []),
                    "expected_contents": item.get("expected_contents", {}),
                    "assertions": item.get("assertions", []),
                    "privacy_public_code": False,
                    "provenance_state": "resolved",
                }
            )
        )
    return cases


def aggregate_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate observed values and evidence rates without inventing data."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        grouped.setdefault(str(run.get("variant", "unknown")), []).append(run)
    result: dict[str, Any] = {"runs": len(runs), "sample_count": len(runs), "variants": {}}
    for variant, items in grouped.items():
        successes = [item for item in items if item.get("success") is True]
        metrics: dict[str, Any] = {
            "runs": len(items),
            "successes": len(successes),
            "success_rate": len(successes) / len(items) if items else None,
            "sample_count": len(items),
            "resolved": len(successes),
            "false_completion": sum(1 for item in items if item.get("false_completion") is True),
            "unsafe_blocked": sum(1 for item in items if item.get("unsafe_blocked") is True),
            "stale_rejected": sum(1 for item in items if item.get("stale_rejected") is True),
            "tamper_or_recovery": sum(1 for item in items if item.get("tamper_or_recovery") is True),
        }
        failure_categories: dict[str, int] = {}
        for item in items:
            category = item.get("failure_category")
            if category:
                failure_categories[str(category)] = failure_categories.get(str(category), 0) + 1
        metrics["failure_taxonomy"] = failure_categories
        check_records = [item for item in items if "check" in item or "required_check_verified" in item]
        checks_passed = sum(
            1
            for item in check_records
            if item.get("required_check_verified") is True
            or (
                isinstance(item.get("check"), dict)
                and item["check"].get("returncode") == 0
            )
        )
        evidence_records = sum(
            1
            for item in items
            if any(
                key in item
                for key in (
                    "required_check_verified",
                    "receipt_verified",
                    "receipt_file_verified",
                    "event_chain_verified",
                )
            )
        )
        usage_records = [item.get("usage") for item in items if isinstance(item.get("usage"), dict)]
        metrics.update(
            {
                "checks": len(check_records),
                "checks_passed": checks_passed,
                "regressions": sum(
                    1
                    for item in items
                    if item.get("regression") is True or item.get("regression_detected") is True
                ),
                "evidence": evidence_records,
                "duration_ms_total": sum(
                    int(item["duration_ms"])
                    for item in items
                    if isinstance(item.get("duration_ms"), (int, float))
                ),
                "tool_calls_total": sum(
                    int(item["tool_calls"])
                    for item in items
                    if isinstance(item.get("tool_calls"), (int, float))
                ),
                "tokens_total": sum(
                    int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
                    for usage in usage_records
                ),
                "cost_usd_total": round(
                    sum(float(usage.get("cost_usd", 0) or 0) for usage in usage_records),
                    8,
                ),
            }
        )
        for key in ("steps", "tool_calls", "duration_ms", "changed_files", "patch_size", "approval_count"):
            values = [item[key] for item in items if isinstance(item.get(key), (int, float))]
            if values:
                metrics[f"mean_{key}"] = sum(values) / len(values)
        for key in (
            "required_check_verified",
            "receipt_verified",
            "receipt_file_verified",
            "event_chain_verified",
            "safety_precondition_passed",
        ):
            values = [item[key] for item in items if isinstance(item.get(key), bool)]
            if values:
                metrics[f"{key}_rate"] = sum(values) / len(values)
        result["variants"][variant] = metrics
    return result


def _estimated_cost(usage: dict[str, Any]) -> float:
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    if input_tokens or output_tokens:
        # Conservative, explicit estimate for a cap; exact provider billing is
        # not available through every Anthropic-compatible endpoint.
        return (input_tokens * 0.003 + output_tokens * 0.015) / 1000
    return max(0.01, int(usage.get("requests", 0) or 0) * 0.01)


def _oracle_edit(case: BenchmarkCase) -> tuple[str, str]:
    """Return the sole deterministic edit for a PatchProof-owned smoke case."""

    if case.source_kind != "local" or case.privacy_public_code:
        raise ValueError(f"case {case.id} cannot use a fixture oracle")
    if len(case.expected_contents) != 1 or len(case.expected_changed_files) != 1:
        raise ValueError(f"case {case.id} must define exactly one expected source edit")
    relative, content = next(iter(case.expected_contents.items()))
    if case.expected_changed_files != [relative]:
        raise ValueError(f"case {case.id} oracle path must exactly match expected_changed_files")
    return relative, content


async def _wait_for_benchmark_task(record: Any, manager: Any) -> None:
    """Wait for a run without auto-approving a risky command."""

    while record.task and not record.task.done():
        if record.status.value == "awaiting_command_approval":
            await manager.cancel(record.id)
            break
        await asyncio.sleep(0.05)
    if record.task:
        try:
            await record.task
        except asyncio.CancelledError:
            pass
