"""agent-eval CLI（Typer 子命令，PRD §70/§71）。

exit code 契约见 Spec V2.1.1 §6.1：
  0 Gate PASS | 1 Gate FAIL | 2 基础设施失败 | 3 无效调用
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from agent_eval import __version__
from agent_eval.cli.commands import (
    baseline_cmd,
    benchmark_cmd,
    case_cmd,
    compare_cmd,
    dataset_cmd,
    experiment_cmd,
    failures_cmd,
    gate_cmd,
    report_cmd,
    review_cmd,
    run_cmd,
    security_cmd,
    serve_cmd,
    storage_cmd,
)

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

app.add_typer(benchmark_cmd.app, name="benchmark")
app.add_typer(case_cmd.app, name="case")
app.add_typer(run_cmd.app, name="run")
app.add_typer(baseline_cmd.app, name="baseline")
app.add_typer(dataset_cmd.app, name="dataset")
app.add_typer(storage_cmd.app, name="storage")
app.add_typer(review_cmd.app, name="review")
app.add_typer(failures_cmd.app, name="failures")
app.add_typer(experiment_cmd.app, name="experiment")
app.add_typer(security_cmd.app, name="security")

app.command("gate")(gate_cmd.gate)
app.command("compare")(compare_cmd.compare)
app.command("report")(report_cmd.report)
app.command("trend")(report_cmd.trend)
app.command("promote")(failures_cmd.promote)
app.command("serve")(serve_cmd.serve)


def _run_async(coro):
    return asyncio.run(coro)


@app.command()
def version() -> None:
    """打印平台版本。"""
    console.print(f"agent-eval {__version__}")


def main() -> None:  # console_scripts entry
    app()


if __name__ == "__main__":
    main()
