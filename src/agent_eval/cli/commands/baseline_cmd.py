"""baseline 命令族（Spec §4.5）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.models.regression import BaselineMode
from agent_eval.storage.baseline_store import BaselineStore

app = typer.Typer(help="Baseline Policy 操作（Spec §4）", no_args_is_help=True)


def _store(data_dir: Path) -> BaselineStore:
    return BaselineStore(data_dir / "state", data_dir / "runs")


def _parse_mode(mode: str) -> BaselineMode:
    try:
        return BaselineMode(mode)
    except ValueError as exc:
        console.print(
            f"[red]error[/red] unknown baseline mode: {mode} "
            f"(expected explicit | release | main-latest)"
        )
        raise typer.Exit(3) from exc


@app.command("show")
def baseline_show(
    benchmark: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    table = Table(title=f"Baselines · {benchmark}")
    for col in ("Mode", "Dataset Version", "Pinned Run", "Pinned By", "Pinned At", "Note"):
        table.add_column(col)
    for baseline in _store(data_dir).show(benchmark):
        table.add_row(
            baseline.mode.value,
            baseline.dataset_version,
            baseline.pinned_run_id or "-",
            baseline.pinned_by or "-",
            baseline.pinned_at.strftime("%m-%d %H:%M"),
            baseline.note,
        )
    console.print(table)


@app.command("resolve")
def baseline_resolve(
    benchmark: str = typer.Argument(...),
    dataset_version: str | None = typer.Option(None, "--dataset-version"),
    mode: str = typer.Option("main-latest", "--mode"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """Spec §4.2 解析算法；未命中 → NO_BASELINE（exit 0，降级不是错误）。"""
    resolved = _store(data_dir).resolve(benchmark, dataset_version, _parse_mode(mode))
    if resolved is None:
        console.print(
            f"[yellow]NO_BASELINE[/yellow] {benchmark}"
            + (f"@{dataset_version}" if dataset_version else "")
        )
        return
    console.print(
        f"[bold]{resolved.mode.value}[/bold] {resolved.benchmark_id}"
        f"@{resolved.dataset_version} → {resolved.pinned_run_id}"
    )


@app.command("pin")
def baseline_pin(
    run_id: str = typer.Argument(...),
    benchmark: str | None = typer.Option(None, "--benchmark"),
    mode: str = typer.Option("explicit", "--mode"),
    note: str = typer.Option("", "--note"),
    pinned_by: str | None = typer.Option(None, "--by"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """Spec §4.5：pin 要求目标 run completed、同 benchmark、Gate PASS。"""
    try:
        baseline = _store(data_dir).pin(
            run_id,
            benchmark_id=benchmark,
            mode=_parse_mode(mode),
            pinned_by=pinned_by,
            note=note,
        )
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    console.print(
        f"pinned {baseline.mode.value} baseline for {baseline.benchmark_id}"
        f"@{baseline.dataset_version} → {baseline.pinned_run_id}"
    )


@app.command("unpin")
def baseline_unpin(
    benchmark: str = typer.Argument(...),
    mode: str = typer.Option("release", "--mode"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    try:
        count = _store(data_dir).unpin(benchmark, _parse_mode(mode))
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    console.print(f"unpinned {count} {mode} baseline(s) for {benchmark}")
