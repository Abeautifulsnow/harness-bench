"""report / trend 命令（Spec §6.2，PRD §58/§70）。

命令名与 PRD §70 对齐：``agent-eval report <run-id>`` / ``agent-eval trend``。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.storage.analytics import Analytics
from agent_eval.storage.run_store import RunStore

ARTIFACTS = ("report.json", "gate.json", "junit.xml", "report.html", "summary.md")


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def report(
    run_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    paths_only: bool = typer.Option(False, "--print-path", help="只打印路径（供 CI artifacts）"),
) -> None:
    """列出 run 的终态产物（Spec §6.2 的五个文件）。"""
    store = RunStore(data_dir / "runs")
    try:
        store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    found = False
    for name in ARTIFACTS:
        path = store.report_path(run_id, name)
        if not path.is_file():
            continue
        found = True
        console.print(str(path) if paths_only else f"{name:12s} {path}")
    if not found:
        console.print(f"[yellow]no artifacts[/yellow] for run {run_id}")
        raise typer.Exit(3)


def trend(
    metric: str | None = typer.Option(
        None, "--metric", help="平台 metric id，如 agent.task_completion"
    ),
    benchmark: str | None = typer.Option(None, "--benchmark"),
    dataset_version: str | None = typer.Option(None, "--dataset-version"),
    limit: int = typer.Option(20, "--limit"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    evals_root: Path = typer.Option(EVALS_ROOT, "--root"),
    rebuild: bool = typer.Option(False, "--rebuild", help="先重建 DuckDB 投影"),
) -> None:
    """PRD §58 Historical Trend：按 commit/version/date/model 展示指标趋势。"""
    with Analytics(data_dir / "analytics.duckdb") as analytics:
        if rebuild:
            analytics.rebuild(data_dir / "runs", evals_root)
        rows = (
            analytics.metric_trend(metric, limit)
            if metric
            else analytics.trend(benchmark, dataset_version, limit)
        )
    if not rows:
        console.print("[yellow]no data[/yellow]（先运行 benchmark，或加 --rebuild）")
        return
    if metric:
        table = Table(title=f"Trend · {metric}")
        for col in ("Run", "Benchmark", "Started", "Model", "Score", "Samples"):
            table.add_column(col)
        for row in rows:
            table.add_row(
                row["run_id"],
                row["benchmark_id"],
                str(row["started_at"]),
                row["agent_model"] or "-",
                _fmt(row["score"]),
                str(row["samples"]),
            )
    else:
        table = Table(title=f"Trend · {benchmark or 'all'}")
        for col in (
            "Run",
            "Benchmark",
            "Commit",
            "Model",
            "Dataset",
            "Task Success",
            "Cases",
            "Tokens",
            "Cost",
            "Verdict",
        ):
            table.add_column(col)
        for row in rows:
            table.add_row(
                row["run_id"],
                row["benchmark_id"],
                row["git_commit"] or "-",
                row["agent_model"] or "-",
                row["dataset_version"],
                f"{float(row['task_success']):.1%}",
                f"{row['passed_cases']}/{row['total_cases']}",
                str(row["total_tokens"]),
                _fmt(row["total_cost"]),
                row["verdict"] or "-",
            )
    console.print(table)
