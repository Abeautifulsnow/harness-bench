"""gate 命令（PRD §64–§69；Spec §6.1/§6.2）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.quality.gates import GATE_KINDS, evaluate_gate, exit_code_for, load_gate_rules
from agent_eval.regression.compare import compare_runs
from agent_eval.reports.aggregate import build_aggregate
from agent_eval.storage.run_store import RunStore


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def gate(
    run_id: str = typer.Argument(...),
    gate_name: str = typer.Option("pr", "--gate", help=" | ".join(GATE_KINDS)),
    rules_root: Path = typer.Option(EVALS_ROOT, "--root"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """重放已存 run 的 Gate 结论（Spec §6.1 exit code 语义）。

    exit 0 = PASS | 1 = FAIL | 2 = 无法可靠评估（存在 ERROR 轮）| 3 = run 不存在。
    """
    store = RunStore(data_dir / "runs")
    try:
        meta, results = store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    rules = _rules(rules_root, gate_name)

    comparison = None
    if meta.baseline_run_id:
        try:
            base_meta, base_results = store.load_run(meta.baseline_run_id)
            comparison = compare_runs(base_meta, base_results, meta, results)
        except AgentEvalError:
            comparison = None
    aggregate = build_aggregate(meta, results, comparison)
    report = evaluate_gate(aggregate, rules, comparison)

    table = Table(title=f"gate {report.gate} · {run_id}")
    for col in ("Rule", "Observed", "Threshold", "Verdict", "Blocking", "Detail"):
        table.add_column(col)
    for rule in report.rules:
        table.add_row(
            rule.rule,
            _fmt(rule.observed),
            _fmt(rule.threshold),
            rule.verdict,
            "yes" if rule.blocking else "no",
            rule.detail,
        )
    console.print(table)
    console.print(f"gate verdict for {run_id}: [bold]{report.verdict}[/bold]")
    for note in report.notes:
        console.print(f"[yellow]note[/yellow] {note}")
    raise typer.Exit(exit_code_for(report, aggregate))


def _rules(rules_root: Path, gate_name: str):
    try:
        return load_gate_rules(rules_root, gate_name)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
