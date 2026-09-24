"""compare 命令（PRD §53/§54/§55/§56/§105）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.regression.compare import compare_runs
from agent_eval.storage.run_store import RunStore

PERFORMANCE_KEYS = ("tool_calls", "tokens", "latency_ms", "cost")


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _delta_percent(base: float | None, cand: float | None) -> str:
    if base in (None, 0) or cand is None:
        return "-"
    return f"{(cand - base) / base * 100:+.2f}%"


def compare(
    baseline_run: str = typer.Argument(..., help="baseline run id"),
    candidate_run: str = typer.Argument(..., help="candidate run id"),
    trace_diff: bool = typer.Option(True, "--trace-diff/--no-trace-diff"),
    json_out: Path | None = typer.Option(None, "--json", help="导出完整比较结果为 JSON"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """PRD §105：Regression/Improved/Flaky + 各项 Diff + Failure Category Diff。

    exit code：0 比较有效 | 3 比较无效（dataset_version / benchmark 不一致，Spec §1.2-3）。
    """
    store = RunStore(data_dir / "runs")
    try:
        base_meta, base_results = store.load_run(baseline_run)
        cand_meta, cand_results = store.load_run(candidate_run)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc

    comparison = compare_runs(
        base_meta,
        base_results,
        cand_meta,
        cand_results,
        trace_loader=(
            _trace_loader(store, base_meta.run_id, cand_meta.run_id) if trace_diff else None
        ),
    )
    _print(comparison)
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(comparison.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"written: {json_out}")
    raise typer.Exit(0 if comparison.valid else 3)


def _trace_loader(store: RunStore, baseline_run_id: str, candidate_run_id: str):
    from agent_eval.trace.builder import TraceBuilder

    def loader(case_run):
        out = {}
        for side, run_id in (("baseline", baseline_run_id), ("candidate", candidate_run_id)):
            events = store.load_events(run_id, f"{case_run.case_id}.iter{case_run.iteration}")
            if not events:
                out[side] = []
                continue
            builder = TraceBuilder()
            builder.feed_all(events)
            out[side] = builder.build().spans
        return out

    return loader


def _print(comparison) -> None:
    if not comparison.valid:
        console.print(f"[red]INVALID[/red] {comparison.invalid_reason}")
        return
    counts = comparison.counts
    console.print(
        f"baseline [bold]{comparison.baseline_run_id}[/bold] → "
        f"candidate [bold]{comparison.candidate_run_id}[/bold]  "
        f"(dataset {comparison.dataset_version}, mode {comparison.baseline_mode})"
    )
    console.print(
        "cases={cases} regression={regression} improved={improved} flaky={flaky} "
        "unchanged={unchanged} undetermined={undetermined}".format(
            **{
                k: counts.get(k, 0)
                for k in ("cases", "regression", "improved", "flaky", "unchanged", "undetermined")
            }
        )
    )
    if comparison.flaky_cases:
        console.print(f"[yellow]FLAKY[/yellow] {', '.join(comparison.flaky_cases)}")
    if comparison.performance_regressions:
        console.print(
            f"[yellow]PERFORMANCE_REGRESSION[/yellow] "
            f"{', '.join(comparison.performance_regressions)}"
        )

    table = Table(title="Run-level Diff（PRD §105）")
    for col in ("Metric", "Baseline", "Candidate", "Δ", "Δ%", "Verdict"):
        table.add_column(col)
    # comparison.metrics 已包含性能/成本项（tokens、tool_calls、latency_ms、cost），
    # 不再另行遍历 baseline_totals 追加，否则同一条 metric 会出现两行。
    for diff in comparison.metrics:
        table.add_row(
            diff.metric,
            _fmt(diff.baseline),
            _fmt(diff.candidate),
            _fmt(diff.delta),
            "-" if diff.delta_percent is None else f"{diff.delta_percent:+.2f}%",
            diff.verdict,
        )
    console.print(table)

    perf = Table(title="Performance Totals（PRD §55 side-by-side）")
    for col in ("Metric", "Baseline", "Candidate", "Δ%"):
        perf.add_column(col)
    for key in PERFORMANCE_KEYS:
        base = comparison.baseline_totals.get(key)
        cand = comparison.candidate_totals.get(key)
        perf.add_row(key, _fmt(base), _fmt(cand), _delta_percent(base, cand))
    console.print(perf)

    if comparison.failure_categories:
        failures = Table(title="Failure Category Diff（PRD §105）")
        for col in ("Category", "Baseline", "Candidate", "Δ"):
            failures.add_column(col)
        for row in comparison.failure_categories:
            failures.add_row(row.category, str(row.baseline), str(row.candidate), f"{row.delta:+d}")
        console.print(failures)

    changed = [
        case
        for case in comparison.cases
        if case.state.value in {"REGRESSION", "IMPROVED", "FLAKY", "INVALID"}
    ]
    if changed:
        cases = Table(title="Case-level")
        for col in ("Case", "Baseline", "Candidate", "State", "Note"):
            cases.add_column(col)
        for case in changed:
            cases.add_row(
                case.case_id,
                case.baseline_stability.value,
                case.candidate_stability.value,
                case.state.value,
                case.invalid_reason or "",
            )
        console.print(cases)

    _print_trace_diffs(comparison)


def _print_trace_diffs(comparison) -> None:
    for diff in comparison.trace_diffs:
        if not diff.changed:
            continue
        console.print(f"[bold]trace diff[/bold] {diff.case_id}")
        for op in diff.tool_sequence:
            marker = {"equal": " ", "added": "+", "removed": "-"}[op.kind]
            console.print(f"  {marker} {op.value}")
        for arg in diff.argument_diffs[:10]:
            console.print(f"  ~ {arg.tool}.{arg.path}: {arg.baseline!r} → {arg.candidate!r}")
        if diff.final_answer_changed:
            console.print("  ~ final answer changed")
