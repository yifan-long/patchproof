"""Health, corpus and evaluation routes with durable report queries."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import __version__
from ..config import settings
from ..corpus import load_cases
from ..evals.orchestrator import EvaluationOptions, EvaluationOrchestrator
from ..evidence.canonical import hash_json
from .common import (
    _APP_ROOT,
    EvaluationRunRequest,
    _corpus_cases,
    _project_data_path,
    _project_manifest_path,
)

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "llm_enabled": settings.llm_enabled,
        "default_repo": str(settings.repo_path_resolved),
        "database": str(settings.database_path_resolved),
        "event_evidence": "sha256-chain",
        "provider": settings.provider_metadata,
        "evaluation": {
            "first_pass_budget_usd": settings.evaluation_first_pass_budget_usd,
            "expansion_budget_usd": settings.evaluation_expansion_budget_usd,
            "max_requests": settings.evaluation_max_requests,
            "max_tokens": settings.evaluation_max_tokens,
        },
    }


@router.get("/suites")
async def list_suites():
    cases = _corpus_cases()
    suites: dict[str, dict[str, object]] = {}
    for case in cases:
        suite = suites.setdefault(case.suite, {"suite": case.suite, "cases": 0, "public_code": False})
        suite["cases"] += 1
        suite["public_code"] = suite["public_code"] or case.privacy_public_code
    return {"schema_version": "patchproof.corpus.v2", "suites": list(suites.values())}


@router.get("/cases")
async def list_cases(suite: str | None = None):
    cases = _corpus_cases()
    if suite:
        cases = [case for case in cases if case.suite == suite]
    return [case.model_dump(mode="json") for case in cases]


@router.get("/preflight")
async def evaluation_preflight(include_public: bool = True):
    cases = _corpus_cases(include_public=include_public)
    return EvaluationOrchestrator(_APP_ROOT).preflight(cases, settings)


@router.post("/runs")
async def trigger_evaluation(request: Request, body: EvaluationRunRequest):
    if not body.confirm_real:
        raise HTTPException(status_code=400, detail="triggering evaluation requires confirm_real=true")
    if body.include_public and not body.confirm_public_code_egress:
        raise HTTPException(status_code=400, detail="public corpus requires confirm_public_code_egress=true")
    if body.confirm_download and not body.confirm_public_code_egress:
        raise HTTPException(status_code=400, detail="download confirmation requires public egress confirmation")
    try:
        cases = load_cases(_project_manifest_path(body.manifest), allow_unresolved=True)
        if body.include_public and not any(case.privacy_public_code for case in cases):
            cases.extend(
                load_cases(_project_manifest_path("benchmarks/public/bugs-in-py.v2.json"), allow_unresolved=True)
            )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid evaluation manifest: {exc}") from exc
    cases = cases[: body.max_cases]
    max_cost_usd = body.max_cost_usd
    if body.budget_stage == "expansion":
        max_cost_usd = settings.evaluation_expansion_budget_usd
    try:
        jsonl_path = _project_data_path(body.jsonl_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report = await EvaluationOrchestrator(_APP_ROOT).run(
        cases,
        settings=settings,
        options=EvaluationOptions(
            repeats=body.repeats,
            max_requests=body.max_requests,
            max_tokens=body.max_tokens,
            max_cost_usd=max_cost_usd,
            confirm_real=body.confirm_real,
            confirm_public_code_egress=body.confirm_public_code_egress,
            confirm_download=body.confirm_download,
        ),
        jsonl_path=jsonl_path,
    )
    report_id = hash_json(report)[:16]
    request.app.state.manager.store.save_evaluation_report(report_id, report)
    return {"report_id": report_id, "report": report}


@router.get("/reports")
async def list_reports(request: Request):
    return [
        {
            "report_id": item["report_id"],
            "evaluation_kind": item["report"].get("evaluation_kind"),
            "runs": len(item["report"].get("runs", [])),
            "aggregate": item["report"].get("aggregate", {}),
        }
        for item in request.app.state.manager.store.list_evaluation_reports()
    ]


@router.get("/reports/{report_id}")
async def get_report(request: Request, report_id: str):
    report = request.app.state.manager.store.get_evaluation_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return report
