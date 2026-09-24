"""benchmark 命令族（PRD §70/§71/§104）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, FIXTURES_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.loading.loader import load_benchmark
from agent_eval.runner.runner import RunConfig, Runner

app = typer.Typer(help="Benchmark 操作", no_args_is_help=True)


@app.command("list")
def benchmark_list(root: Path = typer.Option(EVALS_ROOT, "--root")) -> None:
    benchmarks_dir = root / "benchmarks"
    table = Table(title="Benchmarks")
    for col in ("Name", "Dataset", "Suites", "Default Profile", "Owner"):
        table.add_column(col)
    if benchmarks_dir.is_dir():
        for path in sorted(benchmarks_dir.glob("*.yaml")):
            b = load_benchmark(root, path.stem)
            table.add_row(b.name, b.dataset, ", ".join(b.suites), b.default_profile, b.owner or "-")
    console.print(table)


@app.command("run")
def benchmark_run(
    benchmark: str = typer.Argument(..., help="benchmark name under evals/benchmarks/"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    repeat: int | None = typer.Option(None, "--repeat"),
    concurrency: int = typer.Option(4, "--concurrency"),
    agent_endpoint: str = typer.Option("fake://", "--agent", envvar="AGENT_EVAL_AGENT_ENDPOINT"),
    baseline_policy: str | None = typer.Option(None, "--baseline-policy"),
    baseline_run: str | None = typer.Option(None, "--baseline-run"),
    gate: str = typer.Option("pr", "--gate", help="pr | main | release"),
    run_id_file: Path | None = typer.Option(None, "--run-id-file"),
    no_judge: bool = typer.Option(False, "--no-judge"),
    tag: list[str] = typer.Option([], "--tag"),
    timeout: float | None = typer.Option(None, "--timeout"),
    model: str | None = typer.Option(None, "--model", help="agent model label (PRD §30)"),
    judge_model: str | None = typer.Option(None, "--judge-model"),
    experiment_id: str | None = typer.Option(None, "--experiment-id"),
    variant_id: str | None = typer.Option(None, "--variant-id"),
    root: Path = typer.Option(EVALS_ROOT, "--root"),
    fixtures_root: Path = typer.Option(FIXTURES_ROOT, "--fixtures-root"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """运行 benchmark：Agent → Trace → Eval → Result → Gate（PRD §104 验收链）。"""
    cfg = RunConfig(
        evals_root=root,
        fixtures_root=fixtures_root,
        data_root=data_dir,
        benchmark=benchmark,
        agent_endpoint=agent_endpoint,
        profile=profile,
        repeat=repeat,
        concurrency=concurrency,
        tag_filter=list(tag),
        baseline_policy=baseline_policy,
        baseline_run_id=baseline_run,
        gate=gate,
        no_judge=no_judge,
        timeout=timeout,
        agent_model=model,
        judge_model=judge_model,
        experiment_id=experiment_id,
        variant_id=variant_id,
    )
    try:
        outcome = asyncio.run(Runner(cfg).run(on_run_created=_writer(run_id_file)))
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    except KeyboardInterrupt:
        console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(130) from None
    console.print(
        f"run [bold]{outcome.run_id}[/bold] status={outcome.status} "
        f"gate={outcome.gate.gate if outcome.gate else '-'} verdict={outcome.verdict}"
    )
    if outcome.aggregate is not None and outcome.aggregate.warnings:
        for warning in outcome.aggregate.warnings:
            console.print(f"[yellow]warn[/yellow] {warning}")
    raise typer.Exit(outcome.exit_code)


def _writer(run_id_file: Path | None):
    def write(run_id: str) -> None:
        if run_id_file is not None:
            run_id_file.parent.mkdir(parents=True, exist_ok=True)
            run_id_file.write_text(run_id, encoding="utf-8")  # Spec §6.4

    return write
