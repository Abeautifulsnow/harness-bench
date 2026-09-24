"""failures / promote 命令族（PRD §47–§51，Spec §5.3）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.failures.analysis import analyse_run
from agent_eval.failures.promote import (
    DraftStore,
    build_draft,
    case_run_of,
    load_case_messages,
)
from agent_eval.failures.taxonomy import CATEGORIES, SUBCATEGORIES
from agent_eval.storage.run_store import RunStore

app = typer.Typer(help="Failure Intelligence（PRD §47–§51）", no_args_is_help=True)


def _load(data_dir: Path, run_id: str):
    store = RunStore(data_dir / "runs")
    try:
        return store, store.load_run(run_id)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc


@app.command("list")
def failures_list(
    run_id: str = typer.Argument(...),
    category: str | None = typer.Option(None, "--category", help="二级分类或一级分类"),
    parent: str | None = typer.Option(None, "--parent", help=" | ".join(CATEGORIES)),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    json_out: Path | None = typer.Option(None, "--json"),
) -> None:
    """PRD §47/§48：列出 run 内的 Failure 及其分类与依据。"""
    _, (meta, results) = _load(data_dir, run_id)
    analysis = analyse_run(meta, results)
    failures = analysis.failures
    if category:
        failures = [f for f in failures if f["category"] == category]
    if parent:
        failures = [f for f in failures if f["parent"] == parent]
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"written: {json_out}")
        return
    if not failures:
        console.print("[green]no failures[/green]")
        return
    console.print(
        f"[bold]{run_id}[/bold] failures={len(failures)} categories={analysis.by_category}"
    )
    table = Table(title="Failures")
    for col in ("CaseRun", "Case", "Iter", "Category", "Parent", "Metric", "Reason", "Source"):
        table.add_column(col)
    for failure in failures:
        table.add_row(
            failure["case_run_id"],
            failure["case_id"],
            str(failure["iteration"]),
            failure["category"],
            failure["parent"],
            failure["metric"] or "-",
            (failure["reason"] or "")[:60],
            failure["source"],
        )
    console.print(table)


@app.command("cluster")
def failures_cluster(
    run_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    json_out: Path | None = typer.Option(None, "--json"),
) -> None:
    """PRD §49/§50/§107：聚类并在每个 Cluster 内给出代表 Case。"""
    _, (meta, results) = _load(data_dir, run_id)
    analysis = analyse_run(meta, results)
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(
                [cluster.as_dict() for cluster in analysis.clusters], ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        console.print(f"written: {json_out}")
        return
    if not analysis.clusters:
        console.print("[green]no clusters[/green]")
        return
    table = Table(title=f"Failure Clusters · {run_id}（PRD §49/§50）")
    for col in (
        "Cluster",
        "Category",
        "Size",
        "Representative",
        "Common Tool Sequence",
        "Common Error",
        "Versions",
    ):
        table.add_column(col)
    for cluster in analysis.clusters:
        table.add_row(
            cluster.cluster_id,
            cluster.category,
            str(cluster.size),
            cluster.representative_case_id,
            cluster.common_tool_sequence,
            (cluster.common_error or "")[:50],
            ",".join(str(v) for v in cluster.affected_versions),
        )
    console.print(table)
    console.print(f"parents={analysis.by_parent} tools={analysis.by_tool}")


@app.command("taxonomy")
def failures_taxonomy() -> None:
    """打印平台 Failure Taxonomy（PRD §47）。"""
    table = Table(title="Failure Taxonomy")
    for col in ("Category", "Parent", "Description"):
        table.add_column(col)
    from agent_eval.failures.taxonomy import parent_of

    for category, description in sorted(SUBCATEGORIES.items()):
        table.add_row(category, parent_of(category), description)
    console.print(table)


def promote(
    case_run_id: str = typer.Argument(..., help="CaseRun id（iteration 粒度，Spec §5.3）"),
    run_id: str = typer.Option(..., "--run"),
    suite: str = typer.Option("regression", "--suite"),
    benchmark: str | None = typer.Option(None, "--benchmark"),
    source_type: str = typer.Option("issue", "--source-type"),
    source_ref: str | None = typer.Option(None, "--source-ref"),
    dataset: str | None = typer.Option(
        None, "--dataset", help="用该 dataset 里的原 Case input 作为 Draft 的 input"
    ),
    case_id: str | None = typer.Option(None, "--case-id", help="新 Case id"),
    export: Path | None = typer.Option(None, "--export", help="导出 Case YAML 到该目录"),
    root: Path = typer.Option(EVALS_ROOT, "--root"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """PRD §51 Promote to Benchmark：由失败 CaseRun 生成 Case Draft（不直接改 Suite）。"""
    store, (meta, results) = _load(data_dir, run_id)
    case_run = case_run_of(store, run_id, case_run_id)
    messages = None
    environment: dict = {}
    if dataset:
        catalog, cases = _load_dataset_catalog(root, dataset)
        _ = catalog
        original = next((c for c in cases if c.id == case_run.case_id), None)
        if original is not None:
            messages = original.input.messages()
            environment = {
                "fixture": original.environment.fixture,
                "database": original.environment.database,
            }
    elif any(r.case_id == case_run.case_id for r in results):
        try:
            messages = load_case_messages(root, meta.dataset_id, case_run.case_id)
        except AgentEvalError:
            messages = None

    draft = build_draft(
        meta,
        case_run,
        suite=suite,
        source_type=source_type,
        source_ref=source_ref,
        benchmark_id=benchmark,
        case_id=case_id,
        messages=messages,
        environment=environment,
    )
    path = DraftStore(data_dir / "state").save(draft)
    console.print(f"draft [bold]{draft.id}[/bold] → {path}")
    if not messages:
        console.print(
            "[yellow]warn[/yellow] Draft 的 input 为空（未找到原 Case 定义）："
            "promote 不编造 prompt，请人工补齐后再评审（PRD §15）"
        )
    if export is not None:
        out = DraftStore(data_dir / "state").to_case_yaml(draft.id, export)
        console.print(f"case yaml → {out}")
    raise typer.Exit(0)


def _load_dataset_catalog(root: Path, dataset: str):
    from agent_eval.loading.loader import load_dataset

    try:
        return load_dataset(root, dataset)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc


@app.command("drafts")
def failures_drafts(
    suite: str | None = typer.Option(None, "--suite"),
    status: str | None = typer.Option(None, "--status", help="draft | reviewed | active"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """列出 Case Draft（PRD §15 生命周期）。"""
    records = DraftStore(data_dir / "state").list(suite=suite, status=status)
    if not records:
        console.print("[yellow]no drafts[/yellow]")
        return
    table = Table(title="Case Drafts")
    for col in ("Draft", "Case", "Suite", "Status", "Category", "Source", "CaseRun"):
        table.add_column(col)
    for record in records:
        table.add_row(
            record["id"],
            record["case_id"],
            record["suite"],
            record["draft_status"],
            record.get("failure_category") or "-",
            record.get("source_ref") or record.get("source_type") or "-",
            record["case_run_id"],
        )
    console.print(table)
