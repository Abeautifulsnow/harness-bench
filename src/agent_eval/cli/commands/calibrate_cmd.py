"""calibrate 命令族（Judge Calibration，Spec §26）。

标注工作流：``create``（抽样）→ ``label`` / ``import-labels``（人工标注）→
``evaluate``（一致性快照）。校准台账与 Review Queue 互相独立（Spec §26.1：
词汇与粒度都不同，不得混用）。
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.table import Table

from agent_eval.calibration.agreement import build_snapshot
from agent_eval.calibration.sample import (
    collect_production_items,
    collect_run_items,
    stratified_sample,
)
from agent_eval.calibration.store import CalibrationStore
from agent_eval.cli.commands import DATA_ROOT, console
from agent_eval.errors import AgentEvalError, InvalidCallError
from agent_eval.ids import new_id
from agent_eval.models.calibration import HUMAN_VERDICTS, CalibrationItem, CalibrationSet
from agent_eval.regression.confidence import sample_size_for_precision
from agent_eval.storage.production_store import ProductionStore
from agent_eval.storage.run_store import RunStore

app = typer.Typer(help="Judge Calibration（Spec §26）", no_args_is_help=True)


def _parse_source(source: str) -> tuple[str, str]:
    kind, _, sid = source.partition(":")
    if kind not in ("run", "production") or not sid:
        raise InvalidCallError(
            f"--source 需为 run:<run_id> 或 production:<trace_id>，得到 '{source}'"
        )
    return kind, sid


def _store(data_dir: Path) -> CalibrationStore:
    return CalibrationStore(data_dir / "state")


def _exit_with(exc: AgentEvalError) -> None:
    console.print(f"[red]error[/red] {exc.message}")
    raise typer.Exit(exc.exit_code) from exc


def _agreement_table(title: str, rows) -> Table:
    table = Table(title=title)
    for col in (
        "Metric",
        "N",
        "Accuracy",
        "95% CI",
        "Precision",
        "Recall",
        "Kappa",
        "NearThr",
        "Thr",
    ):
        table.add_column(col)
    for row in rows:
        ci = (
            "-"
            if row.accuracy_ci95 is None
            else f"[{row.accuracy_ci95[0]:.2f}, {row.accuracy_ci95[1]:.2f}]"
        )
        table.add_row(
            row.metric,
            str(row.n),
            f"{row.accuracy:.3f}",
            ci,
            "-" if row.precision is None else f"{row.precision:.3f}",
            "-" if row.recall is None else f"{row.recall:.3f}",
            "-" if row.kappa is None else f"{row.kappa:.3f}",
            "-" if row.near_threshold_accuracy is None else f"{row.near_threshold_accuracy:.3f}",
            "-" if row.threshold is None else f"{row.threshold:g}",
        )
    return table


@app.command("create")
def create(
    source: str = typer.Option(..., "--source", help="run:<run_id> 或 production:<trace_id>"),
    target_n: int = typer.Option(50, "--n", min=1, help="目标样本量（按 metric 分层）"),
    seed: int = typer.Option(42, "--seed", help="抽样种子（同 seed 同样本）"),
    metric: str | None = typer.Option(None, "--metric", help="只抽该 metric"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """从事实源抽样 judge 判定项，建校准集合。"""
    try:
        kind, sid = _parse_source(source)
        if kind == "run":
            meta, results = RunStore(data_dir / "runs").load_run(sid)
            items, excluded = collect_run_items(meta, results)
            judge_model = meta.judge_model
        else:
            evaluations = ProductionStore(data_dir).list_evaluations(sid)
            if not evaluations:
                raise InvalidCallError(f"trace '{sid}' 没有在线评测结果（先执行 evaluate）")
            items, excluded = collect_production_items(sid, evaluations)
            # 评测记录不含 judge model（policy 只存名字）——如实为 None，不猜
            judge_model = None
        if metric:
            items = [i for i in items if i.metric == metric]
        sampled = stratified_sample(items, target_n, seed)
        cal_set = CalibrationSet(
            cal_id=new_id("calset"),
            source_kind=kind,
            source_ids=[sid],
            seed=seed,
            requested_n=target_n,
            judge_model=judge_model,
            items=sampled,
            excluded=excluded,
        )
        path = _store(data_dir).save_set(cal_set)
    except AgentEvalError as exc:
        _exit_with(exc)
        return
    console.print(f"calibration set [bold]{cal_set.cal_id}[/bold] created: {path}")
    counts: dict[str, int] = {}
    for item in cal_set.items:
        counts[item.metric] = counts.get(item.metric, 0) + 1
    table = Table(title=f"样本 {len(cal_set.items)} 条（seed={seed}）")
    for col in ("Metric", "Items"):
        table.add_column(col)
    for name in sorted(counts):
        table.add_row(name, str(counts[name]))
    console.print(table)
    if excluded:
        console.print(f"[yellow]excluded[/yellow] {excluded}（skipped/error/unscored 不进样本）")
    console.print("标注：calibrate label <cal-id> --item <item-id> --verdict pass|fail")
    console.print("或批量：calibrate import-labels <cal-id> labels.yaml")


@app.command("label")
def label(
    cal_id: str = typer.Argument(...),
    item: str = typer.Option(..., "--item", help="item id（calibrate show 可查）"),
    verdict: str = typer.Option(..., "--verdict", help="人工判定：pass|fail"),
    note: str = typer.Option("", "--note"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """标注单条 item（人工判定与 judge 判定同词汇表，Spec §26.2）。"""
    verdict_norm = verdict.strip().lower()
    if verdict_norm not in HUMAN_VERDICTS:
        console.print(f"[red]error[/red] --verdict 需为 {'/'.join(HUMAN_VERDICTS)}")
        raise typer.Exit(3)
    try:
        cal_set = _store(data_dir).load_set(cal_id)
        target = next((i for i in cal_set.items if i.item_id == item), None)
        if target is None:
            raise InvalidCallError(f"set {cal_id} 中不存在 item '{item}'")
        target.human_verdict = verdict_norm  # type: ignore[assignment]
        target.note = note
        _store(data_dir).save_set(cal_set)
    except AgentEvalError as exc:
        _exit_with(exc)
        return
    labeled = sum(1 for i in cal_set.items if i.human_verdict is not None)
    console.print(f"labeled {labeled}/{len(cal_set.items)}（{cal_id}）")


@app.command("import-labels")
def import_labels(
    cal_id: str = typer.Argument(...),
    labels_file: Path = typer.Argument(..., help="YAML：{labels: [{item, verdict, note}]}"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """批量导入标注。全有或全无：任何未知 item / 非法 verdict 都不落盘。"""
    try:
        data = yaml.safe_load(labels_file.read_text(encoding="utf-8"))
    except OSError as exc:
        console.print(f"[red]error[/red] 无法读取 labels 文件：{exc}")
        raise typer.Exit(3) from exc
    except yaml.YAMLError as exc:
        console.print(f"[red]error[/red] labels 文件不是合法 YAML：{exc}")
        raise typer.Exit(3) from exc
    entries = data.get("labels") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        console.print("[red]error[/red] labels 文件需为 {labels: [{item, verdict, note}]} 或裸列表")
        raise typer.Exit(3)
    try:
        cal_set = _store(data_dir).load_set(cal_id)
    except AgentEvalError as exc:
        _exit_with(exc)
        return
    by_id = {i.item_id: i for i in cal_set.items}
    problems: list[str] = []
    updates: list[tuple[CalibrationItem, str, str]] = []
    for entry in entries:
        item_id = str((entry or {}).get("item", ""))
        verdict = str((entry or {}).get("verdict", "")).strip().lower()
        target = by_id.get(item_id)
        if target is None or verdict not in HUMAN_VERDICTS:
            problems.append(item_id or "<empty>")
            continue
        updates.append((target, verdict, str((entry or {}).get("note", ""))))
    if problems:
        console.print(f"[red]error[/red] 未知 item 或非法 verdict：{', '.join(sorted(problems))}")
        raise typer.Exit(3)
    for target, verdict, note in updates:
        target.human_verdict = verdict  # type: ignore[assignment]
        target.note = note
    _store(data_dir).save_set(cal_set)
    labeled = sum(1 for i in cal_set.items if i.human_verdict is not None)
    console.print(f"imported {len(updates)} 条；labeled {labeled}/{len(cal_set.items)}（{cal_id}）")


@app.command("evaluate")
def evaluate(
    cal_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """计算一致性快照（正类 = judge FAIL）并追加进集合历史。"""
    try:
        cal_set = _store(data_dir).load_set(cal_id)
        if not any(i.human_verdict is not None for i in cal_set.items):
            raise InvalidCallError(f"set {cal_id} 还没有任何人工标注（先 label / import-labels）")
        snapshot = build_snapshot(cal_set)
        cal_set.snapshots.append(snapshot)
        _store(data_dir).save_set(cal_set)
    except AgentEvalError as exc:
        _exit_with(exc)
        return
    console.print(
        f"snapshot [bold]{snapshot.snapshot_id}[/bold]"
        f"（labeled {snapshot.labeled}/{snapshot.total}，"
        f"judge_model={snapshot.judge_model or '-'}，positive class=fail）"
    )
    rows = [snapshot.overall, *snapshot.per_metric]
    console.print(_agreement_table(f"Judge vs Human（{cal_id}）", rows))
    if snapshot.labeled < snapshot.total:
        console.print(
            f"[yellow]partial[/yellow] 仅 {snapshot.labeled}/{snapshot.total} 已标注——"
            f"结论只覆盖已标注子集"
        )


@app.command("show")
def show(
    cal_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """查看校准集合：出处、样本、标注进度与最近一次快照。"""
    try:
        cal_set = _store(data_dir).load_set(cal_id)
    except AgentEvalError as exc:
        _exit_with(exc)
        return
    labeled = sum(1 for i in cal_set.items if i.human_verdict is not None)
    sources = ",".join(cal_set.source_ids)
    console.print(
        f"[bold]{cal_set.cal_id}[/bold] source={cal_set.source_kind}:{sources} "
        f"seed={cal_set.seed} judge_model={cal_set.judge_model or '-'} "
        f"labeled {labeled}/{len(cal_set.items)} snapshots={len(cal_set.snapshots)}"
    )
    if cal_set.excluded:
        console.print(f"[yellow]excluded at sampling[/yellow] {cal_set.excluded}")
    table = Table(title="Items")
    for col in ("Item", "Metric", "Case", "Iter", "Score", "Thr", "Judge", "Human", "Near"):
        table.add_column(col)
    for i in cal_set.items:
        table.add_row(
            i.item_id,
            i.metric,
            i.case_id,
            "-" if i.iteration is None else str(i.iteration),
            f"{i.score:.3f}",
            f"{i.threshold:g}",
            i.judge_verdict,
            i.human_verdict or "-",
            "Y" if i.near_threshold else "",
        )
    console.print(table)
    if cal_set.snapshots:
        latest = cal_set.snapshots[-1]
        console.print(
            _agreement_table(
                f"Latest snapshot {latest.snapshot_id}", [latest.overall, *latest.per_metric]
            )
        )


@app.command("list")
def list_sets(
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """列出全部校准集合。"""
    sets = _store(data_dir).list_sets()
    if not sets:
        console.print("no calibration sets")
        return
    table = Table(title="Calibration Sets")
    for col in ("Cal ID", "Source", "Labeled", "Snapshots", "Created"):
        table.add_column(col)
    for cal_set in sets:
        labeled = sum(1 for i in cal_set.items if i.human_verdict is not None)
        table.add_row(
            cal_set.cal_id,
            f"{cal_set.source_kind}:{','.join(cal_set.source_ids)}",
            f"{labeled}/{len(cal_set.items)}",
            str(len(cal_set.snapshots)),
            cal_set.created_at.isoformat(timespec="seconds"),
        )
    console.print(table)


@app.command("plan")
def plan(
    precision: float = typer.Option(0.05, "--precision", help="CI 半宽上限（如 0.05 = ±5pp）"),
    confidence: float = typer.Option(0.95, "--confidence"),
    rate: float = typer.Option(0.5, "--p", help="预期一致率；0.5 为最坏情形上界"),
) -> None:
    """按 CI 精度反推需要标注的样本量（标注是人力成本，先规划再开标）。"""
    required_n = sample_size_for_precision(precision, rate=rate, confidence=confidence)
    if required_n is None:
        console.print("[red]error[/red] --precision 需为正，--p/--confidence 需在 (0,1) 内")
        raise typer.Exit(3)
    console.print(
        f"precision ±{precision:.0%} @ {confidence:.0%} 置信"
        f"（p={rate:g}）需要标注 [bold]{required_n}[/bold] 条 judge 判定"
    )
