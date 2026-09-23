"""agent-eval CLI（Typer 子命令：benchmark/case/run/gate，PRD §70）。

exit code 契约见 Spec V2.1.1 §6.1（P0 生效）：
  0 Gate PASS | 1 Gate FAIL | 2 基础设施失败 | 3 无效调用
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agent_eval import __version__
from agent_eval.errors import AgentEvalError
from agent_eval.loading.loader import load_benchmark, load_dataset
from agent_eval.runner.runner import RunConfig, Runner
from agent_eval.storage.run_store import RunStore

app = typer.Typer(
    name="agent-eval",
    help="Agent Evaluation & Regression Platform (PRD V2.0.1 / Spec V2.1.1)",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console()

DEFAULT_EVALS_ROOT = Path("evals")
DEFAULT_FIXTURES_ROOT = Path("fixtures")
DEFAULT_DATA_ROOT = Path(".agent-eval")


def _run_async(coro):
    return asyncio.run(coro)


def _run_id_writer(run_id_file: Path | None):
    def write(run_id: str) -> None:
        if run_id_file is not None:
            run_id_file.parent.mkdir(parents=True, exist_ok=True)
            run_id_file.write_text(run_id, encoding="utf-8")  # Spec §6.4

    return write


# ----------------------------------------------------------------- benchmark

benchmark_app = typer.Typer(help="Benchmark 操作", no_args_is_help=True)
app.add_typer(benchmark_app, name="benchmark")


@benchmark_app.command("list")
def benchmark_list(
    evals_root: Path = typer.Option(DEFAULT_EVALS_ROOT, "--root"),
) -> None:
    benchmarks_dir = evals_root / "benchmarks"
    table = Table(title="Benchmarks")
    for col in ("Name", "Dataset", "Suites", "Default Profile"):
        table.add_column(col)
    if benchmarks_dir.is_dir():
        for path in sorted(benchmarks_dir.glob("*.yaml")):
            b = load_benchmark(evals_root, path.stem)
            table.add_row(b.name, b.dataset, ", ".join(b.suites), b.default_profile)
    console.print(table)


@benchmark_app.command("run")
def benchmark_run(
    benchmark: str = typer.Argument(..., help="benchmark name under evals/benchmarks/"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    repeat: int | None = typer.Option(None, "--repeat"),
    concurrency: int = typer.Option(4, "--concurrency"),
    agent_endpoint: str = typer.Option("fake://", "--agent", envvar="AGENT_EVAL_AGENT_ENDPOINT"),
    baseline_policy: str | None = typer.Option(None, "--baseline-policy"),
    baseline_run: str | None = typer.Option(None, "--baseline-run"),
    run_id_file: Path | None = typer.Option(None, "--run-id-file"),
    no_judge: bool = typer.Option(False, "--no-judge"),
    tag: list[str] = typer.Option([], "--tag"),
    timeout: float | None = typer.Option(None, "--timeout"),
    evals_root: Path = typer.Option(DEFAULT_EVALS_ROOT, "--root"),
    data_root: Path = typer.Option(DEFAULT_DATA_ROOT, "--data-dir"),
) -> None:
    """运行 benchmark：Agent → Trace → Eval → Result（PRD §104 验收链）。"""
    cfg = RunConfig(
        evals_root=evals_root,
        fixtures_root=DEFAULT_FIXTURES_ROOT,
        data_root=data_root,
        benchmark=benchmark,
        agent_endpoint=agent_endpoint,
        profile=profile,
        repeat=repeat,
        concurrency=concurrency,
        tag_filter=list(tag),
        baseline_policy=baseline_policy,
        baseline_run_id=baseline_run,
        no_judge=no_judge,
        timeout=timeout,
    )
    try:
        outcome = _run_async(Runner(cfg).run(on_run_created=_run_id_writer(run_id_file)))
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    except KeyboardInterrupt:
        console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(130) from None
    console.print(
        f"run [bold]{outcome.run_id}[/bold] status={outcome.status} verdict={outcome.verdict}"
    )
    raise typer.Exit(outcome.exit_code)


# ---------------------------------------------------------------------- case

case_app = typer.Typer(help="Case 查询", no_args_is_help=True)
app.add_typer(case_app, name="case")


def _iter_all_cases(evals_root: Path):
    datasets_dir = evals_root / "datasets"
    if not datasets_dir.is_dir():
        return
    for dataset_dir in sorted(datasets_dir.iterdir()):
        if (dataset_dir / "dataset.yaml").is_file():
            _, cases = load_dataset(evals_root, dataset_dir.name)
            yield dataset_dir.name, cases


@case_app.command("list")
def case_list(evals_root: Path = typer.Option(DEFAULT_EVALS_ROOT, "--root")) -> None:
    table = Table(title="Cases")
    for col in ("Case", "Ver", "Type", "Tags", "Dataset", "Profile"):
        table.add_column(col)
    for dataset_name, cases in _iter_all_cases(evals_root):
        for case in cases:
            table.add_row(
                case.id,
                str(case.version),
                case.input.type,
                ",".join(case.tags),
                dataset_name,
                case.evaluation_profile or "-",
            )
    console.print(table)


@case_app.command("show")
def case_show(
    case_id: str = typer.Argument(...),
    evals_root: Path = typer.Option(DEFAULT_EVALS_ROOT, "--root"),
) -> None:
    for _, cases in _iter_all_cases(evals_root):
        for case in cases:
            if case.id == case_id:
                console.print(case.model_dump_json(indent=2, exclude_none=True))
                return
    console.print(f"[red]error[/red] case not found: {case_id}")
    raise typer.Exit(3)


# ----------------------------------------------------------------------- run

run_app = typer.Typer(help="Run 查询", no_args_is_help=True)
app.add_typer(run_app, name="run")


@run_app.command("list")
def run_list(data_root: Path = typer.Option(DEFAULT_DATA_ROOT, "--data-dir")) -> None:
    store = RunStore(data_root / "runs")
    table = Table(title="Runs")
    for col in ("Run", "Benchmark", "Dataset", "Status", "Baseline", "Started"):
        table.add_column(col)
    for meta in store.list_runs():
        table.add_row(
            meta.run_id,
            meta.benchmark_id,
            f"{meta.dataset_id}@{meta.dataset_version}",
            meta.status.value,
            meta.baseline_mode,
            meta.started_at.strftime("%m-%d %H:%M"),
        )
    console.print(table)


@run_app.command("show")
def run_show(
    run_id: str = typer.Argument(...),
    data_root: Path = typer.Option(DEFAULT_DATA_ROOT, "--data-dir"),
) -> None:
    store = RunStore(data_root / "runs")
    try:
        meta, results = store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    console.print(f"[bold]{meta.run_id}[/bold] {meta.benchmark_id} status={meta.status.value}")
    console.print(
        f"dataset={meta.dataset_id}@{meta.dataset_version} profile={meta.profile} "
        f"baseline={meta.baseline_mode} no_judge={meta.no_judge}"
    )
    if meta.metric_degradations:
        console.print(f"degradations: {meta.metric_degradations}")
    table = Table(title="Case Runs")
    for col in ("Case", "Iter", "Status", "Semantics", "Latency", "Tokens", "Blocking Fail"):
        table.add_column(col)
    for r in sorted(results, key=lambda x: (x.case_id, x.iteration)):
        fails = sum(1 for m in r.metric_results if m.blocking and m.verdict in {"fail", "error"})
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
    report_path = store.root / run_id / "report.json"
    if report_path.is_file():
        console.print(f"report: {report_path}")


# ---------------------------------------------------------------------- gate


@app.command()
def gate(
    run_id: str = typer.Argument(...),
    data_root: Path = typer.Option(DEFAULT_DATA_ROOT, "--data-dir"),
) -> None:
    """重放已存 run 的 Gate 结论（Spec §6.1 exit code 语义）。"""
    from agent_eval.reports.report import compute_verdict

    store = RunStore(data_root / "runs")
    try:
        meta, results = store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    verdict = compute_verdict(meta, results)
    console.print(f"gate verdict for {run_id}: [bold]{verdict}[/bold]")
    raise typer.Exit(0 if verdict == "pass" else (2 if verdict == "infra_failure" else 1))


@app.command()
def version() -> None:
    """打印平台版本。"""
    console.print(f"agent-eval {__version__}")


def main() -> None:  # console_scripts entry
    app()


if __name__ == "__main__":
    main()
