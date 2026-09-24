"""storage 命令族：DuckDB 派生层的重建与查看（Spec §1.3，PRD §79）。

派生层永远是"可重建"的：``rebuild`` 只读取 evals/ 与 .agent-eval/runs/，
删表重建不会丢失任何事实数据。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.storage.analytics import Analytics

app = typer.Typer(help="DuckDB 派生层操作（Spec §1.3）", no_args_is_help=True)


def _open(data_dir: Path) -> Analytics:
    return Analytics(data_dir / "analytics.duckdb")


@app.command("rebuild")
def storage_rebuild(
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    evals_root: Path = typer.Option(EVALS_ROOT, "--root"),
) -> None:
    """从事实层（evals/ + runs/*）重建全部派生表。"""
    with _open(data_dir) as analytics:
        try:
            counts = analytics.rebuild(data_dir / "runs", evals_root)
        except AgentEvalError as exc:
            console.print(f"[red]error[/red] {exc.message}")
            raise typer.Exit(exc.exit_code) from exc
    table = Table(title="Rebuilt Tables")
    for col in ("Table", "Rows"):
        table.add_column(col)
    for name, value in counts.items():
        table.add_row(name, str(value))
    console.print(table)


@app.command("status")
def storage_status(data_dir: Path = typer.Option(DATA_ROOT, "--data-dir")) -> None:
    """查看当前投影行数（不重建）。"""
    with _open(data_dir) as analytics:
        counts = analytics.counts()
    table = Table(title=f"Analytics · {data_dir / 'analytics.duckdb'}")
    for col in ("Table", "Rows"):
        table.add_column(col)
    for name, value in counts.items():
        table.add_row(name, str(value))
    console.print(table)


@app.command("flaky")
def storage_flaky(
    limit: int = typer.Option(50, "--limit"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """PRD §32 Flaky Detection：同一 run 内 PASS/FAIL 混合的 case。"""
    with _open(data_dir) as analytics:
        rows = analytics.flaky_cases(limit)
    if not rows:
        console.print("[green]no flaky cases in projection[/green]")
        return
    table = Table(title="Flaky Cases")
    for col in ("Run", "Case", "Iterations", "Passes", "Fails", "Errors"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            row["run_id"],
            row["case_id"],
            str(row["iterations"]),
            str(row["passes"]),
            str(row["fails"]),
            str(row["errors"]),
        )
    console.print(table)


@app.command("runs")
def storage_runs(
    benchmark: str | None = typer.Option(None, "--benchmark"),
    dataset_version: str | None = typer.Option(None, "--dataset-version"),
    limit: int = typer.Option(50, "--limit"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """投影态 run 列表（PRD §81）。"""
    with _open(data_dir) as analytics:
        rows = analytics.list_runs(benchmark, dataset_version, limit)
    table = Table(title="Runs (projected)")
    for col in ("Run", "Benchmark", "Dataset", "Status", "Verdict", "Cases", "Tokens"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            row["run_id"],
            row["benchmark_id"],
            f"{row['dataset_id']}@{row['dataset_version']}",
            row["status"],
            row["verdict"] or "-",
            f"{row['passed_cases']}/{row['total_cases']}",
            str(row["total_tokens"]),
        )
    console.print(table)
