"""security / red-team 命令族（PRD §62/§63，§108 Release Gate 验收）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.security.redteam import RED_TEAM_CATEGORIES, load_red_team_cases
from agent_eval.security.suites import SUITE_TAGS, list_suites

app = typer.Typer(help="Security / Red Team（PRD §62/§63）", no_args_is_help=True)


@app.command("suites")
def security_suites(root: Path = typer.Option(EVALS_ROOT, "--root")) -> None:
    """列出安全与红队套件（PRD §62/§108：Release Gate 必跑套件）。"""
    table = Table(title="Security Suites")
    for col in ("Suite", "Tag", "Cases", "Description"):
        table.add_column(col)
    for suite in list_suites(root):
        table.add_row(suite.name, suite.tag, str(suite.cases), suite.description)
    console.print(table)


@app.command("report")
def security_report(
    run_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    json_out: Path | None = typer.Option(None, "--json"),
) -> None:
    """汇总某 run 的安全判定（Hard Failure 不可被 judge 覆盖，PRD §63/§110-10）。"""
    from agent_eval.storage.run_store import RunStore

    store = RunStore(data_dir / "runs")
    try:
        _, results = store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    findings = [
        {
            "case_id": result.case_id,
            "case_run_id": result.id,
            "iteration": result.iteration,
            "metric": metric.metric,
            "verdict": metric.verdict,
            "blocking": metric.blocking,
            "reason": metric.reason,
        }
        for result in results
        for metric in result.all_metric_results
        if metric.metric.startswith("security.")
    ]
    failed = [f for f in findings if f["verdict"] == "fail"]
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps({"run_id": run_id, "findings": findings}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"written: {json_out}")
        return
    if not findings:
        console.print(
            "[yellow]no security findings[/yellow]（该 run 未包含安全断言/安全套件 case）"
        )
        return
    table = Table(title=f"Security Findings · {run_id}")
    for col in ("Case", "Iter", "Rule", "Verdict", "Blocking", "Reason"):
        table.add_column(col)
    for finding in findings:
        table.add_row(
            finding["case_id"],
            str(finding["iteration"]),
            finding["metric"],
            finding["verdict"],
            "yes" if finding["blocking"] else "no",
            (finding["reason"] or "")[:60],
        )
    console.print(table)
    console.print(f"hard failures: [bold]{len(failed)}[/bold]")
    raise typer.Exit(1 if failed else 0)


@app.command("redteam")
def security_redteam(root: Path = typer.Option(EVALS_ROOT, "--root")) -> None:
    """列出 Red Team 覆盖矩阵（PRD §62 八类攻击面）。"""
    table = Table(title="Red Team Suite Coverage（PRD §62）")
    for col in ("Attack Surface", "Case", "Declared"):
        table.add_column(col)
    cases = load_red_team_cases(root)
    by_category = {case.category: case for case in cases}
    for category in RED_TEAM_CATEGORIES:
        case = by_category.get(category)
        table.add_row(category, case.case_id if case else "-", "yes" if case else "no")
    console.print(table)
    missing = [c for c in RED_TEAM_CATEGORIES if c not in by_category]
    if missing:
        console.print(f"[yellow]未覆盖[/yellow] {', '.join(missing)}")


_ = (SUITE_TAGS,)
