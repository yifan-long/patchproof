"""FastAPI application assembly: lifespan, middleware and routers.

Route handlers live in ``api_tasks`` (task lifecycle / streaming / receipt) and
``api_evaluation`` (health, corpus, preflight, runs, reports). Shared path and
corpus helpers are in ``api_common``.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api_evaluation import router as evaluation_router
from .api_tasks import router as tasks_router
from .config import settings
from .manager import TaskManager

if sys.platform == "win32":
    # uvicorn --reload spawns its worker through multiprocessing on Windows,
    # which can leave a SelectorEventLoop active. That loop does not implement
    # subprocess support, so every run_check (asyncio.create_subprocess_exec)
    # fails. Pin the Proactor policy explicitly so the app works regardless of
    # how uvicorn starts it.
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


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
app.include_router(tasks_router)
app.include_router(evaluation_router)
