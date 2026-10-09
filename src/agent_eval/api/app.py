"""FastAPI 应用装配（PRD §84 + docs/web-evaluation-control-plane-design.md §41/§42）。

安全边界（§42）由三件事共同保证，缺一不可：
  1. 路由层面：Definition 数据只有 GET；POST 只出现在 eval-runs
     （发起/取消评测）——"Definition mutation = forbidden, Execution verbs = allowed"；
  2. Workspace 持有的 store 只调用读取方法，DuckDB 以 read_only 打开；
     执行侧写入全部经 EvalRunService → Runner 落在 .agent-eval/ 产物区；
  3. 前端构建产物同源托管（``web/dist``），因此不需要 CORS 通配——
     "没有跨源写入口"这件事是配置层面成立的，而不是靠约定。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_eval import __version__
from agent_eval.api.routers import (
    agent_connections,
    analytics,
    automation,
    catalog,
    dashboard,
    eval_runs,
    experiments,
    failures,
    notifications,
    production,
    quality,
)
from agent_eval.api.routers import regressions as regressions_router
from agent_eval.api.routers import runs as runs_router
from agent_eval.api.workspace import Workspace
from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter
from agent_eval.execution.production_monitors import (
    ProductionMonitorService,
    load_production_monitors,
)
from agent_eval.execution.scheduler import SchedulerService, load_schedules
from agent_eval.execution.service import EvalRunService

API_PREFIX = "/api"
STORE_MISSING_TYPES = ("DirectoryNotFound", "IOException", "SerializationException")


def create_app(
    *,
    evals_root: Path,
    data_root: Path,
    fixtures_root: Path | None = None,
    static_dir: Path | None = None,
    max_running_jobs: int = 2,
    production_evaluator: object | None = None,
) -> FastAPI:
    service = EvalRunService(
        evals_root=evals_root,
        data_root=data_root,
        fixtures_root=fixtures_root,
        max_running_jobs=max_running_jobs,
    )
    scheduler = SchedulerService(evals_root=evals_root, data_root=data_root, service=service)
    # P1-1 Production Online Eval 自动化：judge 适配器与 §61 手动评测同一注入契约
    #（create_app 的 production_evaluator 参数），自动与手动共用一条 judge 路径。
    monitors = ProductionMonitorService(
        evals_root=evals_root,
        data_root=data_root,
        evaluator=production_evaluator if production_evaluator is not None else None,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # V2 Scheduled Evaluation：仓库里有调度定义才起守护线程（空定义零开销）。
        if load_schedules(evals_root):
            scheduler.start()
        if load_production_monitors(evals_root):
            monitors.start()
        yield
        # 优雅停机：撤掉在途 Job（Runner 收到真实 cancellation），停执行/调度线程。
        scheduler.stop()
        monitors.stop()
        service.shutdown()

    app = FastAPI(
        title="agent-eval API",
        description=(
            "Agent Evaluation & Regression Platform API（PRD §84 + Web Execution 控制面）。"
            "Definition 数据只读；执行动作（发起/取消评测）经受控入口进入 Runner。"
        ),
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.workspace = Workspace(evals_root=evals_root, data_root=data_root)
    app.state.version = __version__
    app.state.eval_run_service = service
    app.state.scheduler = scheduler
    # §61 Online Eval：judge 适配器可注入（测试用 stub 替身）；缺省走
    # DeepEvalCapabilityAdapter（SDK 惰性加载，未安装时 verdict=skipped）。
    # 注入契约：须实现 async evaluate(metric_id, threshold, trace, model=)
    # -> (score|None, reason) 与 version() -> str|None（返回 None = SDK 缺失，
    # 全部 metric 判 skipped）。
    default_evaluator = (
        production_evaluator if production_evaluator is not None else DeepEvalCapabilityAdapter()
    )
    app.state.production_evaluator = default_evaluator
    monitors.evaluator = default_evaluator
    app.state.production_monitors = monitors

    app.add_middleware(
        CORSMiddleware,
        # 开发期前端跑在 vite dev server（5173），仅白名单本地回环；
        # POST 只服务执行入口（发起/取消评测），依然没有跨源 Definition 写入口。
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    for module in (dashboard, catalog, runs_router, regressions_router):
        app.include_router(module.router, prefix=API_PREFIX)
    for module in (experiments, failures, quality, analytics):
        app.include_router(module.router, prefix=API_PREFIX)
    for module in (eval_runs, agent_connections, automation, notifications, production):
        app.include_router(module.router, prefix=API_PREFIX)

    _register_error_handlers(app)
    _mount_static(app, static_dir)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail, "status": exc.status_code},
        )

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # 只读层不应该 500 一片空白：把异常类型与消息给出，UI 才能显示"平台出了问题"
        return JSONResponse(
            status_code=500,
            content={"error": f"{type(exc).__name__}: {exc}", "status": 500},
        )


def _mount_static(app: FastAPI, static_dir: Path | None) -> None:
    """前端同一进程托管：``/`` 给 SPA，``/api`` 之外的回退到 index.html。"""
    if static_dir is None or not static_dir.is_dir():

        @app.get("/", include_in_schema=False)
        async def root() -> dict[str, str]:
            return {
                "service": "agent-eval API",
                "docs": "/api/docs",
                "note": "web/dist 不存在：先 `cd web && bun run build` 再启动即可同源托管前端",
            }

        return

    index = static_dir / "index.html"
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"unknown endpoint: /{full_path}")
        candidate = static_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
