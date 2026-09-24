"""run 查询命令族（PRD §70/§75）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.models.results import CaseStatus
from agent_eval.reports.aggregate import build_aggregate
from agent_eval.storage.run_store import RunStore

app = typer.Typer(help="Run 查询", no_args_is_help=True)


def _store(data_root: Path) -> RunStore:
    return RunStore(data_root / "runs")


@app.command("list")
def run_list(
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    benchmark: str | None = typer.Option(None, "--benchmark"),
) -> None:
    table = Table(title="Runs")
    for col in ("Run", "Benchmark", "Dataset", "Status", "Verdict", "Baseline", "Started"):
        table.add_column(col)
    for meta in _store(data_dir).list_runs():
        if benchmark and meta.benchmark_id != benchmark:
            continue
        report_path = _store(data_dir).report_path(meta.run_id, "report.json")
        verdict = "-"
        if report_path.is_file():
            import json

            verdict = json.loads(report_path.read_text(encoding="utf-8")).get("verdict", "-")
        table.add_row(
            meta.run_id,
            meta.benchmark_id,
            f"{meta.dataset_id}@{meta.dataset_version}",
            meta.status.value,
            verdict,
            meta.baseline_mode,
            meta.started_at.strftime("%m-%d %H:%M"),
        )
    console.print(table)


@app.command("show")
def run_show(
    run_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    store = _store(data_dir)
    try:
        meta, results = store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    aggregate = build_aggregate(meta, results)
    console.print(f"[bold]{meta.run_id}[/bold] {meta.benchmark_id} status={meta.status.value}")
    console.print(
        f"dataset={meta.dataset_id}@{meta.dataset_version} profile={meta.profile} "
        f"baseline={meta.baseline_mode} ({meta.baseline_run_id or '-'}) no_judge={meta.no_judge}"
    )
    for warning in aggregate.warnings:
        console.print(f"[yellow]warn[/yellow] {warning}")
    if meta.metric_degradations:
        console.print(f"degradations: {meta.metric_degradations}")
    table = Table(title="Case Runs")
    for col in ("Case", "Iter", "Status", "Semantics", "Latency", "Tokens", "Blocking Fail"):
        table.add_column(col)
    for r in sorted(results, key=lambda x: (x.case_id, x.iteration)):
        fails = sum(
            1 for m in r.all_metric_results if m.blocking and m.verdict in {"fail", "error"}
        )
        table.add_row(
            r.case_id,
            str(r.iteration),
            r.status.value,
            r.failure_semantics.value if r.failure_semantics else "-",
            f"{r.latency_ms}ms",
            str(r.token_count),
            str(fails),
        )
    console.print(table)
    report_path = store.report_path(run_id, "report.json")
    if report_path.is_file():
        console.print(f"report: {report_path}")


@app.command("cases")
def run_cases(
    run_id: str = typer.Argument(...),
    status: str | None = typer.Option(None, "--status", help="PASS | FAIL | ERROR"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """列出 run 内的 CaseRun（按状态过滤），供 CI 与人工排查定位。"""
    store = _store(data_dir)
    try:
        meta, results = store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    wanted = status.upper() if status else None
    if wanted and wanted not in {s.value for s in CaseStatus}:
        console.print(f"[red]error[/red] unknown status: {status}")
        raise typer.Exit(3)
    table = Table(title=f"{meta.run_id} case runs")
    for col in ("CaseRun", "Case", "Iter", "Status", "Category", "Error"):
        table.add_column(col)
    for r in sorted(results, key=lambda x: (x.case_id, x.iteration)):
        if wanted and r.status.value != wanted:
            continue
        table.add_row(
            r.id,
            r.case_id,
            str(r.iteration),
            r.status.value,
            r.failure_category or "-",
            (r.error or "")[:60],
        )
    console.print(table)
