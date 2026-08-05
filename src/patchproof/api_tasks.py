"""Task management routes: durable create/query, SSE streaming, receipt
verification, command approval, Apply and cancel."""

from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .models import ApprovalRequest, TaskCreate, TaskStatus
from .receipt import verify_receipt

router = APIRouter()


def _manager(request: Request):
    return request.app.state.manager


@router.get("/tasks")
async def list_tasks(request: Request):
    manager = _manager(request)
    return [
        record.snapshot(chain_head=manager.store.chain_head(record.id)).model_dump(mode="json")
        for record in manager.list()
    ]


@router.post("/tasks")
async def create_task(request: Request, body: TaskCreate):
    manager = _manager(request)
    try:
        record = await manager.create(
            body.goal,
            body.repo_path,
            body.check_command,
            body.max_iterations,
            body.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.snapshot(chain_head=manager.store.chain_head(record.id)).model_dump(mode="json")


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    manager = _manager(request)
    try:
        record = manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return record.snapshot(chain_head=manager.store.chain_head(task_id)).model_dump(mode="json")


@router.get("/tasks/{task_id}/diff")
async def get_diff(request: Request, task_id: str):
    manager = _manager(request)
    try:
        record = manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return {
        "task_id": task_id,
        "diff": record.diff,
        "changed_files": record.changed_files,
        "diff_hash": hashlib.sha256(record.diff.encode("utf-8")).hexdigest(),
    }


@router.get("/tasks/{task_id}/receipt")
async def get_receipt(request: Request, task_id: str):
    manager = _manager(request)
    try:
        manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    receipt = manager.store.get_receipt(task_id)
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


@router.get("/tasks/{task_id}/receipt/verify")
async def verify_task_receipt(request: Request, task_id: str):
    manager = _manager(request)
    try:
        manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    receipt = manager.store.get_receipt(task_id)
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
        "event_chain_verified": manager.verify_chain(task_id),
    }


@router.get("/tasks/{task_id}/events/verify")
async def verify_task_events(request: Request, task_id: str):
    manager = _manager(request)
    try:
        return {
            "task_id": task_id,
            "verified": manager.verify_chain(task_id),
            "head": manager.store.chain_head(task_id),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.post("/tasks/{task_id}/approve-command")
async def approve_command(request: Request, task_id: str, body: ApprovalRequest):
    manager = _manager(request)
    try:
        record = await manager.approve_command(task_id, body.approved, body.approval_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.snapshot(chain_head=manager.store.chain_head(task_id)).model_dump(mode="json")


@router.post("/tasks/{task_id}/apply")
async def apply_task(request: Request, task_id: str):
    manager = _manager(request)
    try:
        record = await manager.apply(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.snapshot(chain_head=manager.store.chain_head(task_id)).model_dump(mode="json")


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str):
    manager = _manager(request)
    try:
        record = await manager.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return record.snapshot(chain_head=manager.store.chain_head(task_id)).model_dump(mode="json")


@router.get("/benchmarks/runs")
async def benchmark_runs(request: Request):
    return _manager(request).store.list_benchmark_runs()


@router.get("/tasks/{task_id}/stream")
async def stream_task(request: Request, task_id: str, after: int = Query(default=0, ge=0)):
    manager = _manager(request)
    try:
        manager.get(task_id)
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
            pending = manager.store.get_events(task_id, after=cursor)
            for event in pending:
                cursor = event.seq
                yield f"id: {event.seq}\ndata: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            record = manager.get(task_id)
            if record.status in terminal_statuses and not manager.store.get_events(task_id, after=cursor):
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
