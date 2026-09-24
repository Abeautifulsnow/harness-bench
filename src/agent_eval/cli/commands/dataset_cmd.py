"""dataset 命令族（PRD §13/§97：Dataset Version 是回归可比性的前提）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, console
from agent_eval.errors import AgentEvalError
from agent_eval.loading.loader import load_dataset
from agent_eval.storage.analytics import Analytics

app = typer.Typer(help="Dataset 版本操作", no_args_is_help=True)


@app.command("list")
def dataset_list(
    root: Path = typer.Option(EVALS_ROOT, "--root"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
    projected: bool = typer.Option(
        False, "--projected", help="改读 DuckDB 定义层投影（需先 storage rebuild）"
    ),
) -> None:
    """Dataset Version：版本号 + 内容 hash 即版本标识（PRD §13）。"""
    if projected:
        with Analytics(data_dir / "analytics.duckdb") as analytics:
            rows = analytics.conn.execute(
                "SELECT dataset_id, version, cases, hash, description "
                "FROM dataset_versions ORDER BY dataset_id"
            ).fetchall()
        table = Table(title="Datasets (projected)")
        for col in ("Dataset", "Version", "Cases", "Hash", "Description"):
            table.add_column(col)
        for row in rows:
            table.add_row(row[0], row[1], str(row[2]), str(row[3])[:12], row[4] or "")
        console.print(table)
        return

    table = Table(title="Datasets")
    for col in ("Dataset", "Version", "Cases", "Hash", "Description"):
        table.add_column(col)
    datasets_dir = root / "datasets"
    if datasets_dir.is_dir():
        for dataset_dir in sorted(datasets_dir.iterdir()):
            if not (dataset_dir / "dataset.yaml").is_file():
                continue
            info, cases = load_dataset(root, dataset_dir.name)
            table.add_row(info.id, info.version, str(len(cases)), info.hash[:12], info.description)
    console.print(table)


@app.command("show")
def dataset_show(
    ref: str = typer.Argument(..., help="<dataset_id> or <dataset_id>@<version>"),
    root: Path = typer.Option(EVALS_ROOT, "--root"),
) -> None:
    try:
        info, cases = load_dataset(root, ref)
    except AgentEvalError as exc:
        console.print(f"[red]error[/red] {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    console.print(f"[bold]{info.id}@{info.version}[/bold] hash={info.hash}")
    table = Table(title="Cases")
    for col in ("Case", "Version", "Type", "Tags"):
        table.add_column(col)
    for case in cases:
        table.add_row(case.id, str(case.version), case.input.type, ",".join(case.tags))
    console.print(table)
