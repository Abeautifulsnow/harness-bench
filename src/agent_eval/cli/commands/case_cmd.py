"""case 查询命令族（PRD §70）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import EVALS_ROOT, console
from agent_eval.loading.loader import load_dataset

app = typer.Typer(help="Case 查询", no_args_is_help=True)


def iter_all_cases(root: Path) -> Iterator[tuple[str, list]]:
    """遍历 evals/datasets/* 的全部 Case（供 case list/show 使用）。"""
    datasets_dir = root / "datasets"
    if not datasets_dir.is_dir():
        return
    for dataset_dir in sorted(datasets_dir.iterdir()):
        if (dataset_dir / "dataset.yaml").is_file():
            _, cases = load_dataset(root, dataset_dir.name)
            yield dataset_dir.name, cases


@app.command("list")
def case_list(root: Path = typer.Option(EVALS_ROOT, "--root")) -> None:
    table = Table(title="Cases")
    for col in ("Case", "Ver", "Type", "Tags", "Dataset", "Profile"):
        table.add_column(col)
    for dataset_name, cases in iter_all_cases(root):
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


@app.command("show")
def case_show(
    case_id: str = typer.Argument(...),
    root: Path = typer.Option(EVALS_ROOT, "--root"),
) -> None:
    for _, cases in iter_all_cases(root):
        for case in cases:
            if case.id == case_id:
                console.print(case.model_dump_json(indent=2, exclude_none=True))
                return
    console.print(f"[red]error[/red] case not found: {case_id}")
    raise typer.Exit(3)
