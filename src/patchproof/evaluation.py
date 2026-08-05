"""Fairness-aware evaluation orchestration and append-only reporting."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_policy import copytree_without_oracles, sanitize_check_output, tree_identity
from .benchmark import BenchmarkCase, BenchmarkHarness, aggregate_metrics
from .budget import BudgetExceeded, BudgetLedger, BudgetLimits
from .config import Settings
from .corpus import SubprocessCommandRunner, build_fetch_plan, execute_fetch_plan
from .docker_executor import DockerEvalExecutor, DockerLimits, DockerProcessAdapter
from .llm import LLMClient, LLMTransportError
from .policy import ProcessExecutor, parse_command
from .repo_index import RepoIndex


class EvaluationOperationalError(RuntimeError):
    """Expected real-evaluation failure carrying only redacted evidence."""

    def __init__(
        self,
        failure_category: str,
        message: str,
        *,
        ledger: dict[str, Any],
        complete_pairs: int = 0,
        partial_records: int = 0,
    ):
        self.failure_category = failure_category
        self.safe_message = message
        self.ledger = ledger
        self.complete_pairs = complete_pairs
        self.partial_records = partial_records
        super().__init__(message)


@dataclass(frozen=True)
class EvaluationOptions:
    repeats: int = 1
    max_requests: int = 40
    max_tokens: int = 32_768
    max_cost_usd: float = 2.0
    reserve_output_tokens: int = 4096
    confirm_real: bool = False
    confirm_public_code_egress: bool = False
    confirm_download: bool = False


class AppendOnlyJSONL:
    """Canonical JSONL writer; existing lines are never rewritten."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")


def canonical_aggregate(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only observed values; partial pairs stay out of comparison."""

    records = list(runs)
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        pair_id = str(record.get("pair_id", f"{record.get('case_id')}:{record.get('repeat', 1)}"))
        by_pair.setdefault(pair_id, []).append(record)
    complete_pairs = [
        items
        for items in by_pair.values()
        if {str(item.get("variant")) for item in items} >= {"baseline", "harness"}
        and all(item.get("status", "completed") in {"completed", "success"} for item in items)
    ]
    comparison_runs = [item for pair in complete_pairs for item in pair]
    return {
        "runs": len(records),
        "partial_runs": len(records) - len(comparison_runs),
        "complete_pairs": len(complete_pairs),
        "head_to_head": aggregate_metrics(comparison_runs),
        "observed": aggregate_metrics(records),
    }


class EvaluationOrchestrator:
    """Run pairs on fresh copies with one shared hard budget ledger."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        docker: DockerEvalExecutor | None = None,
        initial_check_executor: Any | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.docker = docker
        self.initial_check_executor = initial_check_executor

    def preflight(self, cases: list[BenchmarkCase], settings: Settings | None = None) -> dict[str, Any]:
        config = settings or Settings()
        image = next(
            (case.image for case in cases if case.image and not case.image.startswith("local://")),
            config.docker_image,
        )
        docker = self.docker or DockerEvalExecutor(
            image=image,
            docker_cli=config.docker_cli,
            registry=config.docker_registry,
            mirror=config.docker_mirror,
            cache_root=self.project_root / "data" / "eval-cache",
            limits=DockerLimits(
                cpu=config.docker_cpu_limit,
                memory=config.docker_memory_limit,
                pids=config.docker_pids_limit,
                timeout_seconds=config.docker_timeout_seconds,
                output_chars=config.docker_output_limit,
            ),
        )
        case_states = []
        for case in cases:
            plan = build_fetch_plan(case, self.project_root / "data" / "eval-cache", confirm_download=False)
            case_states.append(
                {
                    "case_id": case.id,
                    "source_kind": case.source_kind,
                    "privacy_public_code": case.privacy_public_code,
                    "provenance_state": case.provenance_state,
                    "executable_state": case.executable_state,
                    "python_version": case.python_version,
                    "test_file": case.test_file,
                    "fetch_status": plan.status,
                    "fetch_reason": plan.reason,
                    "resolver_requirements": case.resolver_requirements,
                }
            )
        return {
            "schema_version": "patchproof.preflight.v2",
            "provider": config.provider_metadata,
            "docker": docker.preflight().as_dict(),
            "cases": case_states,
            "public_code_egress": "not attempted",
        }

    async def run(
        self,
        cases: list[BenchmarkCase],
        *,
        settings: Settings | None = None,
        options: EvaluationOptions | None = None,
        model_factory: Callable[[str, BenchmarkCase], Any] | None = None,
        jsonl_path: str | Path | None = None,
    ) -> dict[str, Any]:
        config = settings or Settings()
        opts = options or EvaluationOptions(
            max_requests=config.evaluation_max_requests,
            max_tokens=config.evaluation_max_tokens,
            max_cost_usd=config.evaluation_first_pass_budget_usd,
            reserve_output_tokens=config.evaluation_reserve_output_tokens,
        )
        if opts.repeats < 1:
            raise ValueError("repeats must be >= 1")
        public_cases = [case for case in cases if case.privacy_public_code]
        if public_cases and not opts.confirm_public_code_egress:
            return self._blocked_report(cases, opts, "confirm_public_code_egress_required")
        if public_cases and not opts.confirm_download:
            return self._blocked_report(cases, opts, "confirm_download_required")
        if model_factory is None and not opts.confirm_real:
            return self._blocked_report(cases, opts, "confirm_real_required")
        if public_cases and any(case.provenance_state == "unresolved" for case in public_cases):
            return self._blocked_report(cases, opts, "public_provenance_unresolved")
        if public_cases and any(case.executable_state != "verified_failing" for case in public_cases):
            return self._blocked_report(cases, opts, "public_executable_failure_unverified")
        if model_factory is None:
            docker_state = self.preflight(cases, config)["docker"]
            if docker_state.get("execution_mode") != "docker_isolated":
                return self._blocked_report(cases, opts, "docker_unavailable_no_local_fallback")

        ledger = BudgetLedger(
            BudgetLimits(
                max_requests=opts.max_requests,
                max_input_tokens=opts.max_tokens,
                max_output_tokens=opts.max_tokens,
                max_cost_usd=opts.max_cost_usd,
                cost_per_million_tokens=config.model_cost_per_million_tokens,
                reserve_output_tokens=opts.reserve_output_tokens,
            )
        )
        writer = AppendOnlyJSONL(jsonl_path) if jsonl_path else None
        runs: list[dict[str, Any]] = []
        stop_reason: str | None = None
        for case in cases:
            if case.privacy_public_code and case.provenance_state != "resolved":
                stop_reason = "public_provenance_unresolved"
                break
            source = self._resolve_case_source(case, confirm_download=opts.confirm_download)
            for repeat in range(1, opts.repeats + 1):
                pair_id = f"{case.id}:repeat-{repeat}"
                started = time.perf_counter()
                try:
                    pair = await self._run_pair(case, source, repeat, pair_id, config, model_factory, ledger)
                except BudgetExceeded as exc:
                    stop_reason = f"budget_exhausted:{exc.reason}"
                    break
                except LLMTransportError as exc:
                    aggregate = canonical_aggregate(runs)
                    raise EvaluationOperationalError(
                        _provider_failure_category(exc),
                        _safe_provider_message(exc),
                        ledger=ledger.snapshot(),
                        complete_pairs=int(aggregate["complete_pairs"]),
                        partial_records=int(aggregate["partial_runs"]),
                    ) from exc
                for record in pair:
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    record["duration_ms"] = max(record.get("duration_ms", 0), elapsed_ms)
                    record["ledger"] = ledger.snapshot()
                    runs.append(record)
                    if writer:
                        writer.append(record)
                if any(record.get("status") == "partial" for record in pair):
                    stop_reason = "budget_exhausted"
                    break
            if stop_reason:
                break
        report = {
            "schema_version": "patchproof.evaluation.v2",
            "success": stop_reason is None,
            "evaluation_kind": "model_quality_comparison",
            "execution_policy": {
                "same_task_source_check": True,
                "fresh_isolated_copies": True,
                "repeats": opts.repeats,
                "auto_apply": False,
                "oracle_in_real_path": False,
                "local_smoke_label": "local_smoke_only" if model_factory else None,
            },
            "provider": config.provider_metadata,
            "budget": {**opts.__dict__, "ledger": ledger.snapshot(), "stop_reason": stop_reason},
            "runs": runs,
            "aggregate": canonical_aggregate(runs),
        }
        return report

    async def _run_pair(
        self,
        case: BenchmarkCase,
        source: Path,
        repeat: int,
        pair_id: str,
        settings: Settings,
        model_factory: Callable[[str, BenchmarkCase], Any] | None,
        ledger: BudgetLedger,
    ) -> list[dict[str, Any]]:
        case = case.without_oracle()
        with tempfile.TemporaryDirectory(prefix=f"patchproof-eval-{case.id}-{repeat}-") as directory:
            root = Path(directory)
            baseline_repo = root / "baseline" / "repo"
            harness_repo = root / "harness" / "repo"
            copytree_without_oracles(source, baseline_repo)
            copytree_without_oracles(source, harness_repo)
            harness = BenchmarkHarness(self.project_root)
            docker_executor = None
            if self.initial_check_executor is not None:
                docker_executor = self.initial_check_executor
            elif model_factory is None:
                docker = self.docker or DockerEvalExecutor(
                    image=case.image or settings.docker_image,
                    docker_cli=settings.docker_cli,
                    registry=settings.docker_registry,
                    mirror=settings.docker_mirror,
                    cache_root=self.project_root / "data" / "eval-cache",
                    limits=DockerLimits(
                        cpu=settings.docker_cpu_limit,
                        memory=settings.docker_memory_limit,
                        pids=settings.docker_pids_limit,
                        timeout_seconds=settings.docker_timeout_seconds,
                        output_chars=settings.docker_output_limit,
                    ),
                )
                docker_executor = DockerProcessAdapter(docker)
            elif case.privacy_public_code:
                return self._invalid_gate_records(
                    case,
                    pair_id,
                    repeat,
                    "environment_unreproducible",
                    {"reason": "public evaluation requires an injected Docker executor; host fallback is disabled"},
                    model_factory,
                )

            gate_executor = docker_executor or ProcessExecutor(settings.max_output_chars)
            context, gate_failure = await self._initial_failure_gate(
                case,
                baseline_repo,
                harness_repo,
                gate_executor,
            )
            if gate_failure is not None:
                return self._invalid_gate_records(
                    case,
                    pair_id,
                    repeat,
                    gate_failure,
                    context,
                    model_factory,
                )

            baseline_model = self._model("baseline", case, settings, model_factory, ledger)
            harness_model = self._model("harness", case, settings, model_factory, ledger)
            try:
                baseline = await harness._run_real_baseline(
                    case,
                    baseline_repo,
                    baseline_model,
                    settings,
                    docker_executor,
                    evaluation_context=context,
                )
            except BudgetExceeded as exc:
                return [
                    self._partial_record(case, "baseline", pair_id, repeat, exc, model_factory),
                ]
            baseline.update(
                {
                    "variant": "baseline",
                    "pair_id": pair_id,
                    "repeat": repeat,
                    "status": "completed" if baseline.get("success") else "failed",
                    "execution_mode": ("local_smoke_only" if model_factory else "docker_required"),
                }
            )
            try:
                harness_result = await harness._run_real_harness(
                    case,
                    harness_repo,
                    settings,
                    harness_model,
                    docker_executor,
                    evaluation_context=context,
                )
                harness_result.update(
                    {
                        "variant": "harness",
                        "pair_id": pair_id,
                        "repeat": repeat,
                        "status": "completed" if harness_result.get("success") else "failed",
                        "execution_mode": ("local_smoke_only" if model_factory else "docker_required"),
                    }
                )
            except BudgetExceeded as exc:
                return [
                    baseline,
                    self._partial_record(case, "harness", pair_id, repeat, exc, model_factory),
                ]
            return [baseline, harness_result]

    async def _initial_failure_gate(
        self,
        case: BenchmarkCase,
        baseline_repo: Path,
        harness_repo: Path,
        executor: Any,
    ) -> tuple[dict[str, Any], str | None]:
        first = await self._run_initial_check(case, baseline_repo, executor)
        second = await self._run_initial_check(case, harness_repo, executor)
        first_semantic = {key: value for key, value in first.items() if key != "evidence_sha256"}
        second_semantic = {key: value for key, value in second.items() if key != "evidence_sha256"}
        if first_semantic != second_semantic:
            return {"initial_failure_evidence": first, "comparison": "mismatch"}, "initial_check_nondeterministic"
        evidence = first
        if evidence["returncode"] == 0:
            return {"initial_failure_evidence": evidence}, "initial_check_already_passes"
        if evidence["timed_out"] or evidence["cancelled"]:
            return {"initial_failure_evidence": evidence}, "initial_check_unrunnable"
        output = f"{evidence['stdout']}\n{evidence['stderr']}".lower()
        if any(
            marker in output
            for marker in ("no module named", "modulenotfounderror", "importerror", "command not found", "syntaxerror")
        ):
            return {"initial_failure_evidence": evidence}, "environment_unreproducible"

        index = RepoIndex.build(baseline_repo)
        indexed = set(index.files)
        focus_paths = [case.test_file] if case.test_file else []
        for relative in index.files:
            if relative in evidence["stdout"] or relative in evidence["stderr"]:
                focus_paths.append(relative)
        # A failing output often names the offending function but not its file
        # path (e.g. ``jsonable_encoder() got an unexpected keyword argument``).
        # Files that define a symbol named by the failure are just as likely to
        # hold the defect as files whose path appears literally, so include them
        # in the focused context. Match whole words only so substrings such as
        # ``test`` inside ``tests/...`` do not flood the focus set.
        for symbol in index.symbols:
            if len(symbol.name) >= 4 and (
                re.search(rf"\b{re.escape(symbol.name)}\b", evidence["stdout"])
                or re.search(rf"\b{re.escape(symbol.name)}\b", evidence["stderr"])
            ):
                focus_paths.append(symbol.path)
        if case.test_file and case.test_file not in indexed:
            # The official failing test is introduced by the fix and is absent
            # from this snapshot, so the failure output carries no assertion
            # signal pointing at the defect. Orient the focused context toward
            # the repository's source modules instead of the missing test path.
            focus_paths.extend(
                rel for rel in index.files if rel.endswith(".py") and not rel.startswith("tests/")
            )
        focus_goal = "\n".join((case.goal, case.test_file or "", evidence["stdout"], evidence["stderr"]))
        # Public libraries are larger than the owned mini fixtures; a too-tight
        # context hides the exact source a one-shot baseline must quote (its
        # old_text precondition fails and it hallucinates). Budget enough that
        # goal-token-matched library modules fit below their bug region.
        max_chars = 30_000 if case.privacy_public_code else 24_000
        source_context = index.source_context(
            focus_goal,
            max_files=6 if case.privacy_public_code else 8,
            max_chars=max_chars,
            focus_paths=focus_paths,
        )
        shared_evidence = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "index_context": index.context_for(focus_goal),
            "source_context": (
                f"Initial failing-check evidence:\n{shared_evidence}\n\nFocused source:\n{source_context}"
            ),
            "initial_failure_evidence": evidence,
            "snapshot_identity": evidence["snapshot_sha256"],
            "focus_paths": sorted(set(path for path in focus_paths if path)),
        }, None

    @staticmethod
    async def _run_initial_check(case: BenchmarkCase, repo: Path, executor: Any) -> dict[str, Any]:
        snapshot_sha256 = tree_identity(repo)
        result = await executor.run(
            parse_command(case.required_check_argv),
            cwd=str(repo),
            timeout_seconds=case.timeout,
        )
        raw = result.as_dict()
        evidence = {
            "schema_version": "patchproof.initial-failure.v1",
            "check_argv": list(case.required_check_argv),
            "returncode": int(raw.get("returncode", 1)),
            "stdout": sanitize_check_output(str(raw.get("stdout", "")), workspace=repo),
            "stderr": sanitize_check_output(str(raw.get("stderr", "")), workspace=repo),
            "timed_out": bool(raw.get("timed_out", False)),
            "cancelled": bool(raw.get("cancelled", False)),
            "output_truncated": bool(raw.get("output_truncated", False)),
            "snapshot_sha256": snapshot_sha256,
        }
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence["evidence_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
        return evidence

    @staticmethod
    def _invalid_gate_records(
        case: BenchmarkCase,
        pair_id: str,
        repeat: int,
        category: str,
        evidence: dict[str, Any],
        model_factory: Callable[[str, BenchmarkCase], Any] | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "case_id": case.id,
                "variant": variant,
                "pair_id": pair_id,
                "repeat": repeat,
                "status": "invalid",
                "success": False,
                "failure_category": category,
                "execution_mode": "local_smoke_only" if model_factory else "docker_required",
                "initial_failure_evidence": evidence.get("initial_failure_evidence", evidence),
                "usage": {},
                "duration_ms": 0,
                "steps": 0,
                "tool_calls": 0,
                "changed_files": 0,
                "patch_size": 0,
                "approval_count": 0,
            }
            for variant in ("baseline", "harness")
        ]

    @staticmethod
    def _partial_record(
        case: BenchmarkCase,
        variant: str,
        pair_id: str,
        repeat: int,
        exc: BudgetExceeded,
        model_factory: Callable[[str, BenchmarkCase], Any] | None,
    ) -> dict[str, Any]:
        return {
            "case_id": case.id,
            "variant": variant,
            "pair_id": pair_id,
            "repeat": repeat,
            "status": "partial",
            "success": False,
            "failure_category": "budget_exhausted",
            "error": str(exc),
            "execution_mode": "local_smoke_only" if model_factory else "docker_required",
            "usage": {},
            "duration_ms": 0,
            "steps": 0,
            "tool_calls": 0,
            "changed_files": 0,
            "patch_size": 0,
            "approval_count": 0,
        }

    @staticmethod
    def _model(
        variant: str,
        case: BenchmarkCase,
        settings: Settings,
        factory: Callable[[str, BenchmarkCase], Any] | None,
        ledger: BudgetLedger,
    ) -> Any:
        model = factory(variant, case) if factory else LLMClient(settings, ledger=ledger)
        if factory and hasattr(model, "ledger"):
            model.ledger = ledger
        return model

    def _resolve_case_source(self, case: BenchmarkCase, *, confirm_download: bool) -> Path:
        if case.source_kind == "local":
            candidate = Path(case.local_path or "")
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            if not candidate.is_dir():
                raise ValueError(f"case fixture does not exist: {candidate}")
            return candidate.resolve()
        plan = build_fetch_plan(
            case,
            self.project_root / "data" / "eval-cache",
            confirm_download=confirm_download,
        )
        result = execute_fetch_plan(
            plan,
            runner=SubprocessCommandRunner(),
            confirm_download=confirm_download,
        )
        if result.get("status") != "ready" or not result.get("revision_verified"):
            raise ValueError(f"case {case.id} source cache failed HEAD verification: {result.get('status')}")
        return plan.cache_path.resolve()

    @staticmethod
    def _blocked_report(cases: list[BenchmarkCase], options: EvaluationOptions, reason: str) -> dict[str, Any]:
        return {
            "schema_version": "patchproof.evaluation.v2",
            "success": False,
            "failure_category": "evaluation_preflight_blocked",
            "evaluation_kind": "blocked_preflight",
            "budget": {**options.__dict__, "stop_reason": reason},
            "runs": [],
            "aggregate": canonical_aggregate([]),
            "blocked_cases": [case.id for case in cases],
        }


EvaluationRunner = EvaluationOrchestrator


def _provider_failure_category(exc: LLMTransportError) -> str:
    if exc.category == "provider_output_truncated":
        return "provider_output_truncated"
    if exc.status_code in {401, 402, 403}:
        return "provider_auth_or_credits"
    if exc.status_code == 429:
        return "provider_rate_limit"
    if exc.category == "invalid_json":
        return "provider_invalid_json"
    if exc.status_code is not None and 400 <= exc.status_code < 500:
        return "provider_request_rejected"
    return "provider_failure"


def _safe_provider_message(exc: LLMTransportError) -> str:
    if exc.category == "provider_output_truncated":
        return "provider output reached the configured token limit"
    if exc.status_code in {401, 402, 403}:
        return f"provider rejected credentials or available credits (HTTP {exc.status_code})"
    if exc.status_code == 429:
        return "provider rate limit reached (HTTP 429)"
    if exc.category == "invalid_json":
        return "provider returned invalid JSON"
    if exc.status_code is not None:
        return f"provider request failed (HTTP {exc.status_code})"
    return "provider request failed"
