"""FastAPI 应用装配 —— lifespan、中间件与路由挂载。

做什么
------
把任务路由（api/tasks.py）与评测路由（api/evaluation.py）组装成一个 FastAPI 应用，
并在 lifespan 里创建/关闭 TaskManager（每个 worker 一个，持 SQLite 连接）。

怎么实现
--------
- lifespan：进入时 ``TaskManager(settings)``，退出时关掉 store。
- settings 在启动时从 api 包读取，方便测试 monkeypatch ``patchproof.api.settings``。
- Windows 下显式设 Proactor 事件循环策略。

为什么
------
Windows 上 uvicorn --reload 可能遗留 SelectorEventLoop，它不支持 subprocess，
会导致每次 run_check 抛 NotImplementedError —— 这里显式固定 Proactor 规避。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__
from ..config import settings
from ..task.service import TaskManager
from .evaluation import router as evaluation_router
from .tasks import router as tasks_router

if sys.platform == "win32":
    # uvicorn --reload spawns its worker through multiprocessing on Windows,
    # which can leave a SelectorEventLoop active. That loop does not implement
    # subprocess support, so every run_check (asyncio.create_subprocess_exec)
    # fails. Pin the Proactor policy explicitly so the app works regardless of
    # how uvicorn starts it.
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read settings from the api package at startup so tests that replace
    # ``patchproof.api.settings`` take effect (the module-global below is used
    # only for import-time wiring such as CORS).
    from . import settings as current_settings

    app.state.manager = TaskManager(current_settings)
    yield
    app.state.manager.store.close()


app = FastAPI(title="PatchProof", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(tasks_router)
app.include_router(evaluation_router)
