"""Deterministic infrastructure smoke tests and bounded real-model comparisons.

This module owns the CLI dispatch (``main`` / ``_run_cli``) and the
``BenchmarkHarness`` orchestrator. Pure helpers such as manifest loading,
metrics aggregation and the real-evaluation failure envelope live in
``benchmark_utils`` and are re-exported here so existing imports keep working.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .artifact_policy import copytree_without_oracles
from .benchmark_utils import (  # noqa: F401  (public re-export surface)
    MAX_BASELINE_EDIT_PAYLOAD_BYTES,
    ModelFactory,
    _empty_ledger_snapshot,
    _estimated_cost,
    _oracle_edit,
    _real_failure_envelope,
    _wait_for_benchmark_task,
    _write_json_atomic,
    aggregate_metrics,
    load_cases,
)
from .budget import BudgetExceeded, BudgetLedger, BudgetLimits
from .config import ProviderConfigurationError, Settings
from .llm import FakeLLM, LLMClient, LLMTransportError, LLMUnavailableError, OneShotModel
from .manager import TaskManager
from .models import BenchmarkCase, OneShotResponse
from .policy import ProcessExecutor, classify_argv, parse_command
from .repo_index import RepoIndex
from .runner import AgentRunner
from .storage import SQLiteStore
from .workspace import SnapshotWorkspace, WorkspacePreconditionError


class BenchmarkHarness:
    def __init__(self, project_root: Path, store: SQLiteStore | None = None):
        self.project_root = project_root.resolve()
        self.store = store

    async def run_deterministic_smoke(self, cases: list[BenchmarkCase]) -> dict[str, Any]:
        prepared: list[tuple[BenchmarkCase, dict[str, Any]]] = []
        for case in cases:
            _oracle_edit(case)
            initial = await self._run_initial_fixture_check(case)
            if initial["returncode"] == 0:
                raise ValueError(
                    f"smoke case {case.id} already passes its required check; an actual repair task is required"
                )
            prepared.append((case, initial))

        runs: list[dict[str, Any]] = []
        for case, initial in prepared:
            baseline = await self._run_baseline(case, initial)
            harness = await self._run_harness(case, initial)
            self._persist_run(baseline)
            self._persist_run(harness)
            runs.extend((baseline, harness))
        return {
            "schema_version": "patchproof.benchmark.v2",
            "corpus_schema_version": "patchproof.corpus.v2",
            "evaluation_kind": "infrastructure_validation",
            "description": (
                "Deterministic fail-before/repair/pass-after fixture smoke; validates harness plumbing, "
                "not model quality."
            ),
            "execution_policy": {
                "initial_failure_required": True,
                "exact_oracle_edits_per_case": 1,
                "oracle_scope": "patchproof_owned_local_fixtures_only",
                "oracle_in_real_path": False,
            },
            "initial_checks": [
                {"case_id": case.id, "check": initial}
                for case, initial in prepared
            ],
            "runs": runs,
            "aggregate": aggregate_metrics(runs),
        }

    async def _run_initial_fixture_check(self, case: BenchmarkCase) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix=f"patchproof-initial-{case.id}-") as directory:
            repo = Path(directory) / "repo"
            shutil.copytree(self._resolve_fixture(case), repo)
            result = await ProcessExecutor(case.resources.output_bytes).run(
                parse_command(case.check_command),
                cwd=str(repo),
                timeout_seconds=case.timeout,
            )
            return result.as_dict()

    async def run_real(
        self,
        cases: list[BenchmarkCase],
        settings: Settings | None = None,
        *,
        max_cases: int = 1,
        max_cost_usd: float = 2.0,
        model_factory: ModelFactory | None = None,
        confirm_real: bool = False,
        confirm_public_code_egress: bool = False,
        confirm_download: bool = False,
        docker_executor: Any | None = None,
    ) -> dict[str, Any]:
        """Compare real one-shot and real typed-loop calls on isolated copies.

        This method never reads ``expected_contents`` and never calls Apply.
        ``model_factory`` is intentionally injectable so unit tests can prove
        both model variants are invoked without making a network request.
        """

        if max_cases < 1:
            raise ValueError("real benchmark max_cases 必须 >= 1")
        if max_cost_usd <= 0:
            raise ValueError("real benchmark max_cost_usd 必须 > 0")
        base = settings or Settings()
        selected = cases[:max_cases]
        public_cases = [case for case in selected if case.privacy_public_code]
        if model_factory is None:
            if not confirm_real:
                raise ValueError("real benchmark requires confirm_real=True")
            if public_cases and not confirm_public_code_egress:
                raise ValueError("public corpus requires confirm_public_code_egress=True")
            if public_cases and not confirm_download:
                raise ValueError("public corpus requires confirm_download=True")
            unresolved = [case.id for case in public_cases if case.provenance_state != "resolved"]
            if unresolved:
                raise ValueError(f"public cases require resolver verification: {unresolved}")
            if docker_executor is None:
                from .docker_executor import DockerEvalExecutor, DockerLimits, DockerProcessAdapter

                image = next((case.image for case in selected if case.image), base.docker_image)
                docker = DockerEvalExecutor(
                    image=image,
                    docker_cli=base.docker_cli,
                    registry=base.docker_registry,
                    mirror=base.docker_mirror,
                    cache_root=self.project_root / "data" / "eval-cache",
                    limits=DockerLimits(
                        cpu=base.docker_cpu_limit,
                        memory=base.docker_memory_limit,
                        pids=base.docker_pids_limit,
                        timeout_seconds=base.docker_timeout_seconds,
                        output_chars=base.docker_output_limit,
                    ),
                )
                if docker.preflight().execution_mode != "docker_isolated":
                    raise RuntimeError("Docker isolation is unavailable; real evaluation has no local fallback")
                docker_executor = DockerProcessAdapter(docker)
            execution_mode = "docker_required"
        else:
            execution_mode = "local_smoke_only"
        ledger = BudgetLedger(
            BudgetLimits(
                max_requests=base.evaluation_max_requests,
                max_input_tokens=base.evaluation_max_tokens,
                max_output_tokens=base.evaluation_max_tokens,
                max_cost_usd=max_cost_usd,
                cost_per_million_tokens=base.model_cost_per_million_tokens,
                reserve_output_tokens=base.evaluation_reserve_output_tokens,
            )
        )
        runs: list[dict[str, Any]] = []
        estimated_cost = 0.0
        stopped_reason: str | None = None
        for case in selected:
            if estimated_cost >= max_cost_usd:
                stopped_reason = "estimated_cost_cap"
                break
            real_case = case.without_oracle()
            with tempfile.TemporaryDirectory(prefix=f"patchproof-real-{case.id}-") as directory:
                root = Path(directory)
                source = self._resolve_repo(case)
                baseline_repo = root / "baseline" / "repo"
                harness_repo = root / "harness" / "repo"
                copytree_without_oracles(source, baseline_repo)
                copytree_without_oracles(source, harness_repo)

                from .evaluation import EvaluationOrchestrator

                if real_case.privacy_public_code and model_factory is not None and docker_executor is None:
                    raise RuntimeError("public evaluation has no host fallback for initial-failure evidence")
                gate_executor = docker_executor or ProcessExecutor(base.max_output_chars)
                evaluation_context, gate_failure = await EvaluationOrchestrator(
                    self.project_root,
                    initial_check_executor=gate_executor,
                )._initial_failure_gate(real_case, baseline_repo, harness_repo, gate_executor)
                if gate_failure:
                    runs.extend(
                        EvaluationOrchestrator._invalid_gate_records(
                            real_case,
                            f"{real_case.id}:repeat-1",
                            1,
                            gate_failure,
                            evaluation_context,
                            model_factory,
                        )
                    )
                    continue

                baseline_model = (
                    model_factory("baseline_one_shot_real", real_case)
                    if model_factory
                    else LLMClient(base, ledger=ledger)
                )
                if model_factory and hasattr(baseline_model, "ledger"):
                    baseline_model.ledger = ledger
                try:
                    baseline = await self._run_real_baseline(
                        real_case,
                        baseline_repo,
                        baseline_model,
                        base,
                        docker_executor,
                        evaluation_context=evaluation_context,
                    )
                except BudgetExceeded:
                    stopped_reason = "hard_budget_before_baseline"
                    break
                runs.append(baseline)
                estimated_cost += _estimated_cost(getattr(baseline_model, "usage", {}))
                if estimated_cost >= max_cost_usd:
                    stopped_reason = "estimated_cost_cap_after_baseline"
                    break

                harness_model = (
                    model_factory("harness_tool_loop_real", real_case)
                    if model_factory
                    else LLMClient(base, ledger=ledger)
                )
                if model_factory and hasattr(harness_model, "ledger"):
                    harness_model.ledger = ledger
                try:
                    harness = await self._run_real_harness(
                        real_case,
                        harness_repo,
                        base,
                        harness_model,
                        docker_executor,
                        evaluation_context=evaluation_context,
                    )
                except BudgetExceeded:
                    stopped_reason = "hard_budget_before_harness"
                    break
                runs.append(harness)
                estimated_cost += _estimated_cost(getattr(harness_model, "usage", {}))

        return {
            "schema_version": "patchproof.benchmark.v2",
            "corpus_schema_version": "patchproof.corpus.v2",
            "evaluation_kind": "model_quality_comparison",
            "description": "Real one-shot baseline versus real typed tool-loop on isolated copies; no auto-apply.",
            "execution_policy": {
                "same_task_source_check": True,
                "fresh_isolated_copies": True,
                "auto_apply": False,
                "oracle_in_real_path": False,
                "execution_mode": execution_mode,
            },
            "budget": {
                "max_cases": max_cases,
                "max_cost_usd": max_cost_usd,
                "estimated_cost_usd": round(estimated_cost, 6),
                "stopped_reason": stopped_reason,
                "ledger": ledger.snapshot(),
            },
            "runs": runs,
            "aggregate": aggregate_metrics(runs),
        }

    async def _run_baseline(self, case: BenchmarkCase, initial_check: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="patchproof-baseline-") as directory:
            repo = Path(directory) / "repo"
            shutil.copytree(self._resolve_fixture(case), repo)
            workspace = SnapshotWorkspace(repo, Path(directory) / "staging")
            workspace.create()
            relative, content = _oracle_edit(case)
            target = workspace.staging / relative
            current = target.read_bytes() if target.exists() else b""
            workspace.apply_edit(relative, content, expected_sha256=hashlib.sha256(current).hexdigest())
            result = await ProcessExecutor(case.resources.output_bytes).run(
                parse_command(case.check_command),
                cwd=str(workspace.staging),
                timeout_seconds=case.timeout,
            )
            diff, changed = workspace.diff()
            expected_files_verified = changed == sorted(case.expected_changed_files)
            success = result.returncode == 0 and expected_files_verified and bool(diff)
            return self._metrics(
                case,
                variant="baseline_one_shot",
                success=success,
                steps=1,
                tool_calls=0,
                duration_ms=int((time.perf_counter() - started) * 1000),
                changed_files=changed,
                patch_size=len(diff.encode("utf-8")),
                approval_count=0,
                failure_category=(
                    None
                    if success
                    else "check_failed"
                    if result.returncode != 0
                    else "unexpected_changed_files"
                ),
                usage={},
                extra={
                    "evaluation_kind": "infrastructure_validation",
                    "oracle_fixture_patch": True,
                    "oracle_edit_count": 1,
                    "initial_check": initial_check,
                    "initial_check_failed": initial_check["returncode"] != 0,
                    "check": result.as_dict(),
                    "expected_changed_files_verified": expected_files_verified,
                    "required_check_verified": None,
                    "receipt_verified": None,
                    "receipt_file_verified": None,
                    "event_chain_verified": None,
                    "safety_precondition_passed": True,
                },
            )

    async def _run_harness(self, case: BenchmarkCase, initial_check: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="patchproof-harness-") as directory:
            repo = Path(directory) / "repo"
            shutil.copytree(self._resolve_fixture(case), repo)
            relative, content = _oracle_edit(case)
            current_path = repo / relative
            current = current_path.read_bytes() if current_path.exists() else b""
            actions: list[dict[str, Any]] = [
                {
                    "tool": "apply_edit",
                    "path": relative,
                    "new_text": content,
                    "expected_sha256": hashlib.sha256(current).hexdigest(),
                    "reason": "deterministic benchmark fixture patch",
                }
            ]
            actions.extend(
                [
                    {"tool": "run_check", "argv": parse_command(case.check_command).argv},
                    {"tool": "finish", "summary": "deterministic smoke verified", "verdict": "verified"},
                ]
            )
            db_path = Path(directory) / "benchmark.db"
            settings = Settings(
                repo_path=str(repo),
                database_path=str(db_path),
                max_iterations=3,
                max_tool_steps=20,
                cleanup_workspaces=True,
            )
            store = SQLiteStore(db_path)
            fake = FakeLLM(actions)
            runner = AgentRunner(settings, store=store, llm=fake)
            manager = TaskManager(settings, store=store, runner=runner)
            record = await manager.create(case.goal, str(repo), case.check_command, 3, 20)
            if record.task:
                await record.task
            chain_verified = manager.verify_chain(record.id)
            receipt_verified = bool(record.receipt and record.receipt.verified)
            expected_files_verified = record.changed_files == sorted(case.expected_changed_files)
            tool_sequence = [
                str(event.data["action"]["tool"])
                for event in record.events
                if event.stage == "tool_call"
                and isinstance(event.data.get("action"), dict)
                and isinstance(event.data["action"].get("tool"), str)
            ]
            success = (
                record.status.value == "awaiting_apply"
                and receipt_verified
                and bool(record.receipt and record.receipt.file_verified)
                and chain_verified
                and record.required_check_evidence_valid
                and expected_files_verified
                and bool(record.diff)
                and tool_sequence == ["apply_edit", "run_check", "finish"]
            )
            return self._metrics(
                case,
                variant="harness_tool_loop",
                success=success,
                steps=record.budget_used,
                iterations=record.iteration,
                tool_calls=record.tool_calls,
                duration_ms=int((time.perf_counter() - started) * 1000),
                changed_files=record.changed_files,
                patch_size=len(record.diff.encode("utf-8")),
                approval_count=len(record.approvals),
                failure_category=record.failure_category,
                usage=record.usage,
                extra={
                    "evaluation_kind": "infrastructure_validation",
                    "oracle_fixture_patch": True,
                    "oracle_edit_count": 1,
                    "initial_check": initial_check,
                    "initial_check_failed": initial_check["returncode"] != 0,
                    "check": record.test_result,
                    "expected_changed_files_verified": expected_files_verified,
                    "tool_sequence": tool_sequence,
                    "status": record.status.value,
                    "receipt_hash": record.receipt.receipt_hash if record.receipt else None,
                    "required_check_verified": record.required_check_evidence_valid,
                    "receipt_verified": receipt_verified,
                    "receipt_file_verified": bool(record.receipt and record.receipt.file_verified),
                    "event_chain_verified": chain_verified,
                    "safety_precondition_passed": record.precondition_failures == 0,
                },
            )

    async def _run_real_baseline(
        self,
        case: BenchmarkCase,
        repo: Path,
        model: OneShotModel,
        settings: Settings,
        executor: Any | None = None,
        *,
        evaluation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        workspace = SnapshotWorkspace(repo, repo.parent / "staging", settings.max_file_bytes)
        workspace.create()
        failure_category: str | None = None
        precondition_passed = True
        edit_evidence: list[dict[str, Any]] = []
        one_shot_request_count = 0
        try:
            index = RepoIndex.build(workspace.staging)
            index_context = (
                str(evaluation_context["index_context"])
                if evaluation_context and "index_context" in evaluation_context
                else index.context_for(case.goal)
            )
            source_context = (
                str(evaluation_context["source_context"])
                if evaluation_context and "source_context" in evaluation_context
                else index.source_context(case.goal)
            )
            one_shot_request_count += 1
            proposal = await model.one_shot(
                case.goal,
                index_context,
                source_context,
                case.check_command,
            )
            parsed = OneShotResponse.model_validate(proposal)
            payload_bytes = sum(
                len(edit.path.encode("utf-8"))
                + len(edit.new_text.encode("utf-8"))
                + len((edit.old_text or "").encode("utf-8"))
                for edit in parsed.edits
            )
            if payload_bytes > MAX_BASELINE_EDIT_PAYLOAD_BYTES:
                raise ValueError("one-shot edit payload exceeds the bounded limit")
            allowed = set(case.allowed_edit_paths)
            for edit in parsed.edits:
                if allowed and edit.path not in allowed:
                    raise ValueError(f"one-shot path is outside allowed_edit_paths: {edit.path}")
                if edit.old_text is not None:
                    precondition = "unique_old_text"
                    if edit.expected_sha256:
                        precondition += "+expected_sha256"
                    workspace.apply_edit(
                        edit.path,
                        edit.new_text,
                        expected_sha256=edit.expected_sha256,
                        old_text=edit.old_text,
                        reason="one-shot compact replacement",
                    )
                else:
                    expected = edit.expected_sha256 or workspace.current_sha256(edit.path)
                    precondition = "expected_sha256" if edit.expected_sha256 else "snapshot_sha256"
                    workspace.apply_edit(
                        edit.path,
                        edit.new_text,
                        expected_sha256=expected,
                        reason="one-shot full-file compatibility",
                    )
                edit_evidence.append(
                    {
                        "path": edit.path,
                        "mode": edit.mode,
                        "precondition": precondition,
                    }
                )
        except (ValueError, OSError, WorkspacePreconditionError) as exc:
            failure_category = (
                "invalid_baseline_output" if isinstance(exc, ValueError) else "baseline_precondition_failed"
            )
            precondition_passed = False

        diff, changed = workspace.diff()
        check_data: dict[str, Any] | None = None
        if failure_category is None:
            spec = parse_command(case.check_command)
            decision = classify_argv(spec.argv)
            if decision.requires_approval:
                failure_category = "approval_required"
                check_data = {"argv": spec.argv, "policy": decision.as_dict(), "executed": False}
            else:
                check_executor = executor or ProcessExecutor(settings.max_output_chars)
                result = await check_executor.run(
                    spec,
                    cwd=str(workspace.staging),
                    timeout_seconds=settings.command_timeout_seconds,
                )
                check_data = result.as_dict()
                failure_category = None if result.returncode == 0 else "check_failed"
        return self._metrics(
            case,
            variant="baseline_one_shot_real",
            success=failure_category is None and bool(check_data and check_data.get("returncode") == 0),
            steps=1,
            tool_calls=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            changed_files=changed,
            patch_size=len(diff.encode("utf-8")),
            approval_count=0,
            failure_category=failure_category,
            usage=dict(getattr(model, "usage", {}) or {}),
            extra={
                "evaluation_kind": "model_quality_comparison",
                "model": dict(getattr(model, "metadata", {}) or {}),
                "check_command": case.check_command,
                "check": check_data,
                "auto_apply": False,
                "required_check_verified": None,
                "receipt_verified": None,
                "receipt_file_verified": None,
                "event_chain_verified": None,
                "safety_precondition_passed": precondition_passed,
                "edit_evidence": edit_evidence,
                "one_shot_request_count": one_shot_request_count,
                "initial_failure_evidence": (
                    evaluation_context.get("initial_failure_evidence") if evaluation_context else None
                ),
                "snapshot_identity": evaluation_context.get("snapshot_identity") if evaluation_context else None,
            },
        )

    async def _run_real_harness(
        self,
        case: BenchmarkCase,
        repo: Path,
        settings: Settings,
        model: Any,
        executor: Any | None = None,
        *,
        evaluation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        database = repo.parent / "benchmark.db"
        run_settings = settings.model_copy(
            update={
                "repo_path": str(repo),
                "database_path": str(database),
                "cleanup_workspaces": False,
            }
        )
        store = SQLiteStore(database)
        runner = AgentRunner(
            run_settings,
            store=store,
            llm=model,
            executor=executor,
            evaluation_context=evaluation_context,
        )
        manager = TaskManager(run_settings, store=store, runner=runner)
        record = await manager.create(
            case.goal,
            str(repo),
            case.check_command,
            run_settings.max_iterations,
            run_settings.max_tool_steps,
        )
        if record.task:
            await _wait_for_benchmark_task(record, manager)
        chain_verified = manager.verify_chain(record.id)
        receipt_verified = bool(record.receipt and record.receipt.verified)
        required_verified = record.required_check_evidence_valid
        return self._metrics(
            case,
            variant="harness_tool_loop_real",
            success=(
                record.status.value == "awaiting_apply"
                and required_verified
                and receipt_verified
                and bool(record.receipt and record.receipt.file_verified)
                and chain_verified
            ),
            steps=record.budget_used,
            iterations=record.iteration,
            tool_calls=record.tool_calls,
            duration_ms=int((time.perf_counter() - started) * 1000),
            changed_files=record.changed_files,
            patch_size=len(record.diff.encode("utf-8")),
            approval_count=len(record.approvals),
            failure_category=record.failure_category,
            usage=record.usage,
            extra={
                "evaluation_kind": "model_quality_comparison",
                "model": dict(getattr(model, "metadata", {}) or {}),
                "status": record.status.value,
                "receipt_hash": record.receipt.receipt_hash if record.receipt else None,
                "auto_apply": False,
                "required_check_verified": required_verified,
                "receipt_verified": receipt_verified,
                "receipt_file_verified": bool(record.receipt and record.receipt.file_verified),
                "event_chain_verified": chain_verified,
                "safety_precondition_passed": record.precondition_failures == 0,
                "initial_failure_evidence": (
                    evaluation_context.get("initial_failure_evidence") if evaluation_context else None
                ),
                "snapshot_identity": evaluation_context.get("snapshot_identity") if evaluation_context else None,
            },
        )

    def _resolve_fixture(self, case: BenchmarkCase) -> Path:
        candidate = Path(case.fixture)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        if not candidate.is_dir():
            raise ValueError(f"benchmark fixture 不存在: {candidate}")
        return candidate.resolve()

    def _persist_run(self, metrics: dict[str, Any]) -> None:
        if self.store is None:
            self.store = SQLiteStore(self.project_root / "data" / "patchproof.db")
        run_id = self.store.create_benchmark_run(metrics["case_id"], metrics["variant"])
        self.store.finish_benchmark_run(
            run_id,
            status="success" if metrics["success"] else "failed",
            metrics=metrics,
            report=metrics,
        )

    def _resolve_repo(self, case: BenchmarkCase) -> Path:
        candidate = Path(case.repo or case.fixture)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        if not candidate.is_dir():
            raise ValueError(f"benchmark repo 不存在: {candidate}")
        return candidate.resolve()

    @staticmethod
    def _metrics(
        case: BenchmarkCase,
        *,
        variant: str,
        success: bool,
        steps: int,
        tool_calls: int,
        duration_ms: int,
        iterations: int | None = None,
        changed_files: list[str],
        patch_size: int,
        approval_count: int,
        failure_category: str | None,
        usage: dict[str, Any],
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "case_id": case.id,
            "variant": variant,
            "goal": case.goal,
            "check_command": case.check_command,
            "success": success,
            "steps": steps,
            "iterations": iterations if iterations is not None else 1,
            "tool_calls": tool_calls,
            "duration_ms": duration_ms,
            "changed_files": len(changed_files),
            "changed_file_paths": changed_files,
            "patch_size": patch_size,
            "approval_count": approval_count,
            "usage": usage,
            "failure_category": failure_category,
            **extra,
        }


def _run_cli(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    if args.command == "resolve-public":
        if not args.confirm_download:
            raise SystemExit("public provenance resolution requires --confirm-download")
        from .docker_executor import DockerEvalExecutor
        from .evaluator_image import load_evaluator_image_lock
        from .public_resolver import PublicProvenanceResolver

        execution_probe = None
        if args.image_lock:
            try:
                image_lock = load_evaluator_image_lock(args.image_lock)
                execution_probe = DockerEvalExecutor(
                    image=image_lock["immutable_reference"],
                    docker_cli=args.docker_cli,
                    cache_root=args.cache_root,
                )
            except (OSError, ValueError, json.JSONDecodeError):
                execution_probe = None

        report = PublicProvenanceResolver(
            args.cache_root,
            execution_probe=execution_probe,
            dataset_root=args.dataset_root,
            dataset_revision=args.dataset_revision,
        ).resolve(
            args.manifest,
            output=args.output,
            image_lock=args.image_lock,
            confirm_download=True,
        )
        print(json.dumps({
            "resolved": sum(item["status"] == "resolved" for item in report["resolutions"]),
            "unresolved": sum(item["status"] != "resolved" for item in report["resolutions"]),
            "output": str(Path(args.output).resolve()),
            "model_calls": 0,
            "public_code_llm_egress": False,
        }, ensure_ascii=False, indent=2))
        return
    if args.command == "build-evaluator-image":
        from .evaluator_image import EvaluatorImageBuilder

        result = EvaluatorImageBuilder(docker_cli=args.docker_cli).build(
            context=args.context,
            dockerfile=args.dockerfile,
            base_image=args.base_image,
            tag=args.tag,
            output=args.output,
            requirements_lock=args.requirements_lock,
            acr_registry=args.acr_registry,
            pip_index_url=args.pip_index_url,
            confirm_build=args.confirm_build,
        )
        print(json.dumps({
            "output": str(Path(args.output).resolve()),
            "immutable_reference": result.lock["immutable_reference"],
            "image_id": result.lock["image_id"],
        }, ensure_ascii=False, indent=2))
        return
    if args.command == "faults":
        from .faults import run_offline_faults

        report = run_offline_faults(args.output)
    else:
        cases = load_cases(args.manifest)
    if args.command == "smoke":
        report = asyncio.run(BenchmarkHarness(project_root).run_deterministic_smoke(cases))
    elif args.command == "preflight":
        from .evaluation import EvaluationOrchestrator

        report = EvaluationOrchestrator(project_root).preflight(cases, Settings())
    elif args.command == "real":
        if not args.confirm_real:
            raise SystemExit("real benchmark 会产生模型调用，请显式传入 --confirm-real")
        if any(case.privacy_public_code for case in cases) and not args.confirm_public_code_egress:
            raise SystemExit("public corpus requires --confirm-public-code-egress")
        selected = cases[: args.max_cases]
        settings: Settings | None = None
        max_cost_usd = (
            float(args.max_cost_usd)
            if args.max_cost_usd is not None
            else 20.0 if args.budget_stage == "expansion" else 2.0
        )
        from .evaluation import EvaluationOperationalError, EvaluationOptions, EvaluationOrchestrator

        try:
            settings = Settings()
            stage_budget = (
                settings.evaluation_expansion_budget_usd
                if args.budget_stage == "expansion"
                else settings.evaluation_first_pass_budget_usd
            )
            max_cost_usd = args.max_cost_usd if args.max_cost_usd is not None else stage_budget
            print(
                json.dumps(
                    {
                        "provider": settings.provider_metadata,
                        "task_count": len(selected),
                        "repeats": args.repeats,
                        "max_requests": args.max_requests,
                        "max_tokens": args.max_tokens,
                        "max_cost_usd": max_cost_usd,
                        "budget_stage": args.budget_stage,
                    },
                    ensure_ascii=False,
                )
            )
            if not settings.llm_enabled:
                raise LLMUnavailableError("provider API key is not configured")
            report = asyncio.run(
                EvaluationOrchestrator(project_root).run(
                    selected,
                    settings=settings,
                    options=EvaluationOptions(
                        repeats=args.repeats,
                        max_requests=args.max_requests,
                        max_tokens=args.max_tokens,
                        max_cost_usd=max_cost_usd,
                        confirm_real=args.confirm_real,
                        confirm_public_code_egress=args.confirm_public_code_egress,
                        confirm_download=args.confirm_download,
                    ),
                    jsonl_path=args.jsonl,
                )
            )
        except (
            LLMTransportError,
            LLMUnavailableError,
            BudgetExceeded,
            ValidationError,
            ProviderConfigurationError,
            EvaluationOperationalError,
        ) as exc:
            reserve = settings.evaluation_reserve_output_tokens if settings else 4096
            provider = settings.provider_metadata if settings else {
                "provider": "unavailable",
                "transport": "unavailable",
                "api_key_configured": False,
            }
            ledger = getattr(exc, "ledger", None) or getattr(exc, "snapshot", None)
            if not isinstance(ledger, dict):
                ledger = _empty_ledger_snapshot(
                    max_requests=args.max_requests,
                    max_tokens=args.max_tokens,
                    max_cost_usd=max_cost_usd,
                    reserve_output_tokens=reserve,
                    cost_per_million_tokens=settings.model_cost_per_million_tokens if settings else 0.0,
                )
            if isinstance(exc, EvaluationOperationalError):
                category = exc.failure_category
                message = exc.safe_message
                complete_pairs = exc.complete_pairs
                partial_records = exc.partial_records
            elif isinstance(exc, BudgetExceeded):
                category = "budget_exhausted"
                message = f"hard evaluation budget exhausted: {exc.reason}"
                complete_pairs = 0
                partial_records = 0
            elif isinstance(exc, LLMTransportError):
                from .evaluation import _provider_failure_category, _safe_provider_message

                category = _provider_failure_category(exc)
                message = _safe_provider_message(exc)
                complete_pairs = 0
                partial_records = 0
            else:
                category = "provider_configuration"
                message = "provider configuration is unavailable or invalid"
                complete_pairs = 0
                partial_records = 0
            failure = _real_failure_envelope(
                category=category,
                message=message,
                provider=provider,
                budget_stage=args.budget_stage,
                max_requests=args.max_requests,
                max_tokens=args.max_tokens,
                max_cost_usd=max_cost_usd,
                reserve_output_tokens=reserve,
                selected_case_ids=[case.id for case in selected],
                repeats=args.repeats,
                ledger=ledger,
                complete_pairs=complete_pairs,
                partial_records=partial_records,
            )
            _write_json_atomic(args.output, failure)
            print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(2) from None

        if report.get("success") is False:
            budget = report.get("budget", {})
            aggregate = report.get("aggregate", {})
            stop_reason = str(budget.get("stop_reason") or report.get("failure_category") or "evaluation_failed")
            category = (
                "budget_exhausted"
                if stop_reason.startswith("budget_exhausted")
                else "evaluation_preflight_blocked"
            )
            message = (
                "hard evaluation budget exhausted"
                if category == "budget_exhausted"
                else f"real evaluation blocked by preflight: {stop_reason}"
            )
            failure = _real_failure_envelope(
                category=category,
                message=message,
                provider=settings.provider_metadata,
                budget_stage=args.budget_stage,
                max_requests=args.max_requests,
                max_tokens=args.max_tokens,
                max_cost_usd=max_cost_usd,
                reserve_output_tokens=settings.evaluation_reserve_output_tokens,
                selected_case_ids=[case.id for case in selected],
                repeats=args.repeats,
                ledger=budget.get("ledger"),
                complete_pairs=int(aggregate.get("complete_pairs", 0)),
                partial_records=int(aggregate.get("partial_runs", 0)),
            )
            _write_json_atomic(args.output, failure)
            print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(2)
    output = Path(args.output)
    _write_json_atomic(output, report)
    print(json.dumps(report.get("aggregate", report), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="PatchProof evaluation harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--manifest", required=True)
    smoke.add_argument("--output", required=True)
    smoke.add_argument("--project-root", default=".")
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--manifest", required=True)
    preflight.add_argument("--output", required=True)
    preflight.add_argument("--project-root", default=".")
    faults = subparsers.add_parser("faults")
    faults.add_argument("--output", required=True)
    faults.add_argument("--project-root", default=".")
    resolver = subparsers.add_parser("resolve-public")
    resolver.add_argument("--manifest", required=True)
    resolver.add_argument("--output", default="data/bugs-in-py.resolved.lock.json")
    resolver.add_argument("--cache-root", default="data/eval-cache")
    resolver.add_argument("--image-lock", default=None)
    resolver.add_argument("--dataset-root", default=None)
    resolver.add_argument("--dataset-revision", default=None)
    resolver.add_argument("--project-root", default=".")
    resolver.add_argument("--docker-cli", default="docker")
    resolver.add_argument("--confirm-download", action="store_true")
    image = subparsers.add_parser("build-evaluator-image")
    image.add_argument("--context", default="docker/evaluator")
    image.add_argument("--dockerfile", default="docker/evaluator/Dockerfile")
    image.add_argument("--requirements-lock", default="docker/evaluator/requirements.lock")
    image.add_argument("--base-image", required=True)
    image.add_argument("--tag", default="patchproof-evaluator:0.3.7")
    image.add_argument("--output", default="data/evaluator-image.lock.json")
    image.add_argument("--acr-registry", default=None)
    image.add_argument("--pip-index-url", default=None)
    image.add_argument("--docker-cli", default="docker")
    image.add_argument("--project-root", default=".")
    image.add_argument("--confirm-build", action="store_true")
    real = subparsers.add_parser("real")
    real.add_argument("--manifest", required=True)
    real.add_argument("--output", required=True)
    real.add_argument("--project-root", default=".")
    real.add_argument("--confirm-real", action="store_true")
    real.add_argument("--confirm-public-code-egress", action="store_true")
    real.add_argument("--confirm-download", action="store_true")
    real.add_argument("--budget-stage", choices=("first-pass", "expansion"), default="first-pass")
    real.add_argument("--max-cases", type=int, default=1)
    real.add_argument("--repeats", type=int, default=1)
    real.add_argument("--max-requests", type=int, default=40)
    real.add_argument("--max-tokens", type=int, default=32768)
    real.add_argument("--max-cost-usd", type=float, default=None)
    real.add_argument("--jsonl", default="data/evaluation-runs.jsonl")
    args = parser.parse_args()
    _run_cli(args)


if __name__ == "__main__":
    main()
