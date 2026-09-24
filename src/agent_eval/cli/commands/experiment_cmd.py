"""experiment 命令族（PRD §22–§28/§70/§106）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, FIXTURES_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.experiment.models import ExperimentMatrix
from agent_eval.experiment.runner import run_experiment, variant_matrix
from agent_eval.experiment.store import ExperimentStore

app = typer.Typer(help="Experiment 操作（PRD §22–§28）", no_args_is_help=True)


def _store(data_dir: Path) -> ExperimentStore:
    return ExperimentStore(data_dir / "state")


@app.command("create")
def experiment_create(
    name: str = typer.Argument(...),
    benchmark: str = typer.Option(..., "--benchmark"),
    matrix_file: Path | None = typer.Option(
        None, "--matrix", help="实验矩阵 YAML（PRD §24：matrix: {model: [...], prompt: [...]}）"
    ),
    variant_file: Path | None = typer.Option(
        None, "--variants", help="显式 variant 列表 YAML（PRD §23）"
    ),
    dataset_version: str = typer.Option("", "--dataset-version"),
    profile: str | None = typer.Option(None, "--profile"),
    repeat: int | None = typer.Option(None, "--repeat"),
    concurrency: int = typer.Option(4, "--concurrency"),
    gate: str = typer.Option("pr", "--gate"),
    baseline_run: str | None = typer.Option(None, "--baseline-run"),
    note: str = typer.Option("", "--note"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """创建实验：矩阵自动展开为 variants（PRD §24/§106）。"""
    if matrix_file is None and variant_file is None:
        console.print("[red]error[/red] 需要 --matrix 或 --variants 之一")
        raise typer.Exit(3)
    try:
        matrix = _load_matrix(matrix_file) if matrix_file else None
        variants = _load_variants(variant_file) if variant_file else None
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    experiment = _store(data_dir).create(
        name,
        benchmark,
        matrix=matrix,
        variants=variants,
        profile=profile,
        repeat=repeat,
        concurrency=concurrency,
        gate=gate,
        baseline_run_id=baseline_run,
        baseline_policy="explicit" if baseline_run else None,
        dataset_version=dataset_version,
        note=note,
    )
    console.print(
        f"experiment [bold]{experiment.id}[/bold] {experiment.name} "
        f"variants={len(experiment.variants)}"
    )
    for variant in experiment.variants:
        console.print(f"  {variant.id}  {variant.label()}")


def _load_matrix(path: Path) -> ExperimentMatrix:
    data = _read_yaml(path)
    raw = data.get("matrix") if "matrix" in data else data
    if not isinstance(raw, dict):
        raise AgentEvalError(f"matrix file must contain a 'matrix' mapping: {path}")
    axes: dict[str, list] = {}
    for axis, values in raw.items():
        if not isinstance(values, list) or not values:
            raise AgentEvalError(f"matrix axis '{axis}' must be a non-empty list: {path}")
        axes[str(axis)] = values
    return ExperimentMatrix(axes=axes, base=dict(data.get("base") or {}))


def _load_variants(path: Path) -> list[dict]:
    data = _read_yaml(path)
    raw = data.get("variants") if "variants" in data else data
    if not isinstance(raw, list) or not raw:
        raise AgentEvalError(f"variants file must contain a non-empty list: {path}")
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AgentEvalError(f"variant #{index} must be a mapping: {path}")
        out.append(dict(item.get("dimensions", item)))
    return out


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise AgentEvalError(f"definition file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise AgentEvalError(f"definition file must contain a mapping: {path}")
    return data


@app.command("list")
def experiment_list(data_dir: Path = typer.Option(DATA_ROOT, "--data-dir")) -> None:
    table = Table(title="Experiments")
    for col in ("Experiment", "Name", "Benchmark", "Status", "Variants", "Created"):
        table.add_column(col)
    for experiment in _store(data_dir).list():
        table.add_row(
            experiment.id,
            experiment.name,
            experiment.benchmark_id,
            experiment.status.value,
            str(len(experiment.variants)),
            experiment.created_at.strftime("%m-%d %H:%M"),
        )
    console.print(table)


@app.command("show")
def experiment_show(
    experiment_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    try:
        experiment = _store(data_dir).load(experiment_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    console.print(
        f"[bold]{experiment.id}[/bold] {experiment.name} status={experiment.status.value} "
        f"benchmark={experiment.benchmark_id}"
    )
    table = Table(title="Variants")
    for col in ("Variant", "Name", "Dimensions", "Status", "Run"):
        table.add_column(col)
    for variant in experiment.variants:
        table.add_row(
            variant.id,
            variant.name,
            json.dumps(variant.dimensions, ensure_ascii=False),
            variant.status,
            variant.run_id or "-",
        )
    console.print(table)


@app.command("run")
def experiment_run(
    experiment_id: str = typer.Argument(...),
    agent: str = typer.Option("fake://", "--agent", help="endpoint 模板，支持 {model} 等占位"),
    root: Path = typer.Option(EVALS_ROOT, "--root"),
    fixtures_root: Path = typer.Option(FIXTURES_ROOT, "--fixtures-root"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    json_out: Path | None = typer.Option(None, "--json"),
) -> None:
    """对同一 Dataset 执行全部 variant（PRD §106）。"""
    store = _store(data_dir)
    try:
        experiment = store.load(experiment_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    try:
        outcome = run_experiment(
            experiment,
            evals_root=root,
            fixtures_root=fixtures_root,
            data_root=data_dir,
            agent_template=agent,
            store=store,
        )
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    _print_matrix(outcome)
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(
                [
                    {
                        "variant_id": r.variant_id,
                        "name": r.name,
                        "dimensions": r.dimensions,
                        "run_id": r.run_id,
                        "status": r.status,
                        "verdict": r.verdict,
                        "metrics": r.metrics,
                        "counts": r.counts,
                        "cost": r.cost,
                        "avg_latency_ms": r.avg_latency_ms,
                        "avg_tokens": r.avg_tokens,
                        "error": r.error,
                    }
                    for r in outcome.results
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        console.print(f"written: {json_out}")
    raise typer.Exit(outcome.exit_code)


def _print_matrix(outcome) -> None:
    console.print(
        f"experiment [bold]{outcome.experiment.id}[/bold] status={outcome.experiment.status.value}"
    )
    table = Table(title="Variant Matrix（PRD §106）")
    for col in (
        "Variant",
        "Dimensions",
        "Status",
        "Verdict",
        "Task Success",
        "Tool Correctness",
        "Cost",
        "Latency",
    ):
        table.add_column(col)
    for result in outcome.results:
        table.add_row(
            result.variant_id,
            " / ".join(f"{k}={v}" for k, v in sorted(result.dimensions.items())) or "-",
            result.status,
            result.verdict,
            _pct(result.metrics.get("task_success")),
            _num(result.metrics.get("agent.tool_correctness")),
            "-" if result.cost is None else f"{result.cost:.4f}",
            f"{result.avg_latency_ms:.0f}ms",
        )
    console.print(table)
    for dimension in sorted({k for r in outcome.results for k in r.dimensions}):
        groups = variant_matrix(outcome.results, dimension)
        if len(groups) < 2:
            continue
        console.print(f"[bold]by {dimension}[/bold]")
        for key, items in sorted(groups.items()):
            best = max(items, key=lambda r: r.metrics.get("task_success", 0.0))
            console.print(
                f"  {key or '(unset)'}: n={len(items)} "
                f"best task_success={_pct(best.metrics.get('task_success'))} ({best.name})"
            )


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"
