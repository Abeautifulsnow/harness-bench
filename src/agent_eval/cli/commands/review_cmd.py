"""review 命令族（PRD §60/§61，Spec §5.2/§5.4）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.review.store import NOTE_REQUIRED, VERDICTS, ReviewStore, queue_candidates
from agent_eval.storage.run_store import RunStore

app = typer.Typer(help="Human Review（PRD §60/§61）", no_args_is_help=True)


def _store(data_dir: Path) -> ReviewStore:
    return ReviewStore(data_dir / "state")


@app.command("list")
def review_list(
    run: str | None = typer.Option(None, "--run"),
    status: str | None = typer.Option(None, "--status", help="pending"),
    queue_reason: str | None = typer.Option(None, "--queue-reason"),
    queue: bool = typer.Option(False, "--queue", help="按 PRD §61 计算应入队的 case（不写库）"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """列出人工结论与评审队列。"""
    if queue:
        if run is None:
            console.print("[red]error[/red] --queue 需要 --run <run-id>")
            raise typer.Exit(3)
        records = _store(data_dir).list(run_id=run)
        reviewed = {record.get("case_id") for record in records}
        store = RunStore(data_dir / "runs")
        try:
            meta, results = store.load_run(run)
        except AgentEvalError as exc:
            console.print(f"[red]error[/red] {exc.message}")
            raise typer.Exit(exc.exit_code) from exc
        candidates = [
            candidate
            for candidate in queue_candidates(meta, results)
            if candidate["case_id"] not in reviewed
        ]
        if not candidates:
            console.print("[green]review queue empty[/green]")
            return
        table = Table(title=f"Review Queue · {run}（PRD §61）")
        for col in ("Case", "Machine Verdict", "Queue Reason", "Reasons", "Stability"):
            table.add_column(col)
        for candidate in candidates:
            table.add_row(
                candidate["case_id"],
                candidate["machine_verdict"],
                candidate["queue_reason"],
                ", ".join(candidate["reasons"]),
                candidate["stability"],
            )
        console.print(table)
        return

    reason_filter = queue_reason
    records = _store(data_dir).list(run_id=run, status=status, queue_reason=reason_filter)
    if not records:
        console.print("[yellow]no human reviews[/yellow]")
        return
    table = Table(title="Human Reviews")
    for col in ("Review", "Run", "Case", "Verdict", "Reviewer", "Queue Reason", "Note"):
        table.add_column(col)
    for record in records:
        table.add_row(
            record.get("id", "-"),
            record.get("run_id", "-"),
            record.get("case_id", "-"),
            record.get("verdict", "-"),
            record.get("reviewer") or "-",
            record.get("queue_reason") or "-",
            (record.get("note") or "")[:50],
        )
    console.print(table)


@app.command("show")
def review_show(
    run_id: str = typer.Argument(...),
    case_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """展示某 case 的机器结论与人工结论（两者并存，PRD §60）。"""
    store = RunStore(data_dir / "runs")
    try:
        meta, results = store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    iterations = [r for r in results if r.case_id == case_id]
    if not iterations:
        console.print(f"[red]error[/red] case not in run: {case_id}")
        raise typer.Exit(3)
    from agent_eval.regression.stability import compute_stability
    from agent_eval.review.store import _machine_verdict

    stability = compute_stability(case_id, iterations)
    console.print(
        f"[bold]{run_id}[/bold] / {case_id}  machine_verdict="
        f"{_machine_verdict(iterations)} stability={stability.stability.value} "
        f"pass_rate={stability.pass_rate:.0%}"
    )
    table = Table(title="Iterations")
    for col in ("Iter", "Status", "Semantics", "Category", "Blocking Failures"):
        table.add_column(col)
    for result in iterations:
        failures = [
            f"{m.metric}@{m.metadata.get('mount') or '-'}: {m.reason}"
            for m in result.all_metric_results
            if m.blocking and m.verdict in {"fail", "error"}
        ]
        table.add_row(
            str(result.iteration),
            result.status.value,
            result.failure_semantics.value if result.failure_semantics else "-",
            result.failure_category or "-",
            "; ".join(failures) or "-",
        )
    console.print(table)
    record = _store(data_dir).effective(run_id, case_id).get((run_id, case_id))
    if record:
        console.print(
            f"human_verdict=[bold]{record['verdict']}[/bold] "
            f"by={record.get('reviewer') or '-'} note={record.get('note') or '-'}"
        )
    else:
        console.print("[yellow]no human verdict yet[/yellow]")


def _verdict_command(
    run_id: str,
    case_id: str,
    verdict: str,
    note: str,
    reviewer: str | None,
    queue_reason: str | None,
    data_dir: Path,
) -> None:
    try:
        record = _store(data_dir).add(
            run_id,
            case_id,
            verdict,
            reviewer=reviewer,
            note=note,
            queue_reason=queue_reason,
        )
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    console.print(f"recorded {record['verdict']} for {run_id}/{case_id}（人工结论不覆盖机器结果）")


@app.command("verdict")
def review_verdict(
    run_id: str = typer.Argument(...),
    case_id: str = typer.Argument(...),
    verdict: str = typer.Argument(..., help=" | ".join(VERDICTS)),
    note: str = typer.Option("", "--note"),
    reviewer: str | None = typer.Option(None, "--reviewer"),
    queue_reason: str | None = typer.Option(None, "--queue-reason"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """记录人工 verdict（PRD §60 五值；EXPECTED/FALSE_* 必须带 --note）。"""
    _verdict_command(run_id, case_id, verdict, note, reviewer, queue_reason, data_dir)


def _make_alias(verdict: str):
    def _cmd(
        run_id: str = typer.Argument(...),
        case_id: str = typer.Argument(...),
        note: str = typer.Option("", "--note"),
        reviewer: str | None = typer.Option(None, "--reviewer"),
        queue_reason: str | None = typer.Option(None, "--queue-reason"),
        data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    ) -> None:
        _verdict_command(run_id, case_id, verdict, note, reviewer, queue_reason, data_dir)

    _cmd.__doc__ = f"等价于 verdict {verdict}（Spec §5.2 便捷别名）"
    return _cmd


for _name, _verdict in (
    ("pass", "PASS"),
    ("fail", "FAIL"),
    ("expected", "EXPECTED"),
):
    app.command(_name)(_make_alias(_verdict))

__all__ = ["app", "NOTE_REQUIRED"]
