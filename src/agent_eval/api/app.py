"""FastAPI 应用装配（PRD §84）。

只读约束由三件事共同保证，缺一不可：
  1. 本模块只注册 GET 路由；
  2. Workspace 持有的 store 只调用读取方法，DuckDB 以 read_only 打开；
  3. 前端构建产物同源托管（``web/dist``），因此不需要 CORS 通配——
     "没有跨源写入口"这件事是配置层面成立的，而不是靠约定。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_eval import __version__
from agent_eval.api.routers import analytics, catalog, dashboard, experiments, failures, quality
from agent_eval.api.routers import regressions as regressions_router
from agent_eval.api.routers import runs as runs_router
from agent_eval.api.workspace import Workspace

API_PREFIX = "/api"
STORE_MISSING_TYPES = ("DirectoryNotFound", "IOException", "SerializationException")


def create_app(
    *,
    evals_root: Path,
    data_root: Path,
    static_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(
        title="agent-eval API",
        description=(
            "Agent Evaluation & Regression Platform 只读 API（PRD §84）。"
            "所有写入路径留在 CLI：HTTP 层不存在任何改数据的入口。"
        ),
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.workspace = Workspace(evals_root=evals_root, data_root=data_root)
    app.state.version = __version__

    app.add_middleware(
        CORSMiddleware,
        # 开发期前端跑在 vite dev server（5173），仅白名单本地回环
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    for module in (dashboard, catalog, runs_router, regressions_router):
        app.include_router(module.router, prefix=API_PREFIX)
    for module in (experiments, failures, quality, analytics):
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
