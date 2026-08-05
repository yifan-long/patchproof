from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .corpus import load_cases
from .evaluation import EvaluationOptions, EvaluationOrchestrator
from .manager import TaskManager
from .models import ApprovalRequest, TaskCreate, TaskStatus
from .receipt import verify_receipt


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.manager = TaskManager(settings)
    app.state.evaluation_reports = {}
    yield
    app.state.manager.store.close()


app = FastAPI(title="PatchProof", version="0.3.1", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def manager() -> TaskManager:
    return app.state.manager


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


def _project_path(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parents[2] / candidate
    return candidate.resolve()


def _project_data_path(value: str) -> Path:
    candidate = _project_path(value)
    root = (Path(__file__).resolve().parents[2] / "data").resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("evaluation report path must stay under patchproof/data")
    return candidate


def _project_manifest_path(value: str) -> Path:
    """Resolve evaluation manifests without allowing arbitrary file reads."""

    candidate = _project_path(value)
    root = Path(__file__).resolve().parents[2]
    if candidate != root and root not in candidate.parents:
        raise ValueError("evaluation manifest must stay under patchproof")
    return candidate


def _corpus_cases(*, include_public: bool = True) -> list[Any]:
    root = Path(__file__).resolve().parents[2]
    cases = load_cases(root / "benchmarks" / "manifest.v2.json")
    if include_public:
        cases.extend(load_cases(root / "benchmarks" / "public" / "bugs-in-py.v2.json"))
    return cases


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.3.1",
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


@app.get("/suites")
async def list_suites():
    cases = _corpus_cases()
    suites: dict[str, dict[str, Any]] = {}
    for case in cases:
        suite = suites.setdefault(case.suite, {"suite": case.suite, "cases": 0, "public_code": False})
        suite["cases"] += 1
        suite["public_code"] = suite["public_code"] or case.privacy_public_code
    return {"schema_version": "patchproof.corpus.v2", "suites": list(suites.values())}


@app.get("/cases")
async def list_cases(suite: str | None = None):
    cases = _corpus_cases()
    if suite:
        cases = [case for case in cases if case.suite == suite]
    return [case.model_dump(mode="json") for case in cases]


@app.get("/preflight")
async def evaluation_preflight(include_public: bool = True):
    cases = _corpus_cases(include_public=include_public)
    return EvaluationOrchestrator(Path(__file__).resolve().parents[2]).preflight(cases, settings)


@app.post("/runs")
async def trigger_evaluation(body: EvaluationRunRequest):
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
    report = await EvaluationOrchestrator(Path(__file__).resolve().parents[2]).run(
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
    report_id = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    app.state.evaluation_reports[report_id] = report
    return {"report_id": report_id, "report": report}


@app.get("/reports")
async def list_reports():
    return [
        {
            "report_id": report_id,
            "evaluation_kind": report.get("evaluation_kind"),
            "runs": len(report.get("runs", [])),
            "aggregate": report.get("aggregate", {}),
        }
        for report_id, report in app.state.evaluation_reports.items()
    ]


@app.get("/reports/{report_id}")
async def get_report(report_id: str):
    report = app.state.evaluation_reports.get(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return report


@app.get("/tasks")
async def list_tasks():
    return [
        record.snapshot(chain_head=manager().store.chain_head(record.id)).model_dump(mode="json")
        for record in manager().list()
    ]


@app.post("/tasks")
async def create_task(body: TaskCreate):
    try:
        record = await manager().create(
            body.goal,
            body.repo_path,
            body.check_command,
            body.max_iterations,
            body.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.snapshot(chain_head=manager().store.chain_head(record.id)).model_dump(mode="json")


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    try:
        record = manager().get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return record.snapshot(chain_head=manager().store.chain_head(task_id)).model_dump(mode="json")


@app.get("/tasks/{task_id}/diff")
async def get_diff(task_id: str):
    try:
        record = manager().get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return {
        "task_id": task_id,
        "diff": record.diff,
        "changed_files": record.changed_files,
        "diff_hash": hashlib.sha256(record.diff.encode("utf-8")).hexdigest(),
    }


@app.get("/tasks/{task_id}/receipt")
async def get_receipt(task_id: str):
    try:
        manager().get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    receipt = manager().store.get_receipt(task_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="任务尚未生成 Patch Receipt")
    return {
        "task_id": task_id,
        "receipt_hash": receipt.receipt_hash,
        "verified": verify_receipt(receipt.receipt, receipt.receipt_hash) and receipt.file_verified,
        "logical_verified": verify_receipt(receipt.receipt, receipt.receipt_hash),
        "artifact_path": receipt.artifact_path,
        "artifact_file_sha256": receipt.file_sha256,
        "artifact_file_verified": receipt.file_verified,
        "receipt": receipt.receipt,
    }


@app.get("/tasks/{task_id}/receipt/verify")
async def verify_task_receipt(task_id: str):
    try:
        manager().get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    receipt = manager().store.get_receipt(task_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="任务尚未生成 Patch Receipt")
    return {
        "task_id": task_id,
        "receipt_hash": receipt.receipt_hash,
        "verified": verify_receipt(receipt.receipt, receipt.receipt_hash) and receipt.file_verified,
        "logical_verified": verify_receipt(receipt.receipt, receipt.receipt_hash),
        "artifact_path": receipt.artifact_path,
        "artifact_file_sha256": receipt.file_sha256,
        "artifact_file_verified": receipt.file_verified,
        "event_chain_verified": manager().verify_chain(task_id),
    }


@app.get("/tasks/{task_id}/events/verify")
async def verify_task_events(task_id: str):
    try:
        return {
            "task_id": task_id,
            "verified": manager().verify_chain(task_id),
            "head": manager().store.chain_head(task_id),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/tasks/{task_id}/approve-command")
async def approve_command(task_id: str, body: ApprovalRequest):
    try:
        record = await manager().approve_command(task_id, body.approved, body.approval_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.snapshot(chain_head=manager().store.chain_head(task_id)).model_dump(mode="json")


@app.post("/tasks/{task_id}/apply")
async def apply_task(task_id: str):
    try:
        record = await manager().apply(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.snapshot(chain_head=manager().store.chain_head(task_id)).model_dump(mode="json")


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    try:
        record = await manager().cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return record.snapshot(chain_head=manager().store.chain_head(task_id)).model_dump(mode="json")


@app.get("/benchmarks/runs")
async def benchmark_runs():
    return manager().store.list_benchmark_runs()


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request, after: int = Query(default=0, ge=0)):
    try:
        manager().get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    last_event_id = request.headers.get("last-event-id")
    cursor = max(after, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)

    async def events():
        nonlocal cursor
        terminal_statuses = {
            TaskStatus.AWAITING_APPLY,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.FAILED_RECOVERABLE,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
        }
        while True:
            pending = manager().store.get_events(task_id, after=cursor)
            for event in pending:
                cursor = event.seq
                yield f"id: {event.seq}\ndata: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            record = manager().get(task_id)
            if record.status in terminal_statuses and not manager().store.get_events(task_id, after=cursor):
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
