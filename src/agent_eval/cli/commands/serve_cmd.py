"""serve 命令：启动只读 REST API / Web UI 后端（PRD §72–§78, §84，P5）。

API 是只读投影层：它读 evals/ 定义树、runs 产物与 DuckDB 派生层，不写任何事实数据。
Web UI 的所有数据都从这里来，因此不存在"UI 绕过契约直接改数据"的路径。
"""

from __future__ import annotations

from pathlib import Path

import typer

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, console

DEFAULT_STATIC = Path("web/dist")


def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
    root: Path = typer.Option(EVALS_ROOT, "--root"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    static_dir: Path = typer.Option(
        DEFAULT_STATIC, "--static", help="前端构建产物目录（web/dist），存在时同源托管"
    ),
) -> None:
    """启动 API 服务（PRD §84）。前端构建产物存在时同源托管，避免 CORS 配置。"""
    try:
        import uvicorn

        from agent_eval.api.app import create_app
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的可执行报错
        console.print(f"[red]error[/red] serve 需要 api 依赖：`uv sync --extra api`（{exc}）")
        raise typer.Exit(3) from exc

    app = create_app(evals_root=root, data_root=data_dir, static_dir=static_dir)
    console.print(f"agent-eval API on http://{host}:{port}  (data={data_dir})")
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")
