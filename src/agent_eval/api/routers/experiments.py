"""Experiment 只读接口（PRD §22–§28/§74/§106）。

variant 的对比列直接来自各 variant 对应 run 的 report.json（PRD §81：Run 级数字一律是
CaseRun 聚合），因此实验表格与 Run Detail 页永远不会互相矛盾。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from agent_eval.api.deps import WorkspaceDep
from agent_eval.api.schemas import ExperimentDetail, ExperimentRow, VariantRow
from agent_eval.errors import AgentEvalError
from agent_eval.experiment.models import Experiment
from agent_eval.experiment.runner import variant_matrix
from agent_eval.storage.run_store import RunStore

router = APIRouter(tags=["experiments"])

# PRD §74 的对比列 → report.json 的取数位置
_METRIC_KEYS = {
    "task_success": ("metrics", "task_success"),
    "task_completion": ("metrics", "agent.task_completion"),
    "step_ratio": ("metrics", "native.step_ratio"),
    "argument_checks": ("metrics", "native.argument_checks"),
    "tool_calls": ("metrics", "tool_calls"),
    "tokens": ("metrics", "tokens"),
    "latency_ms": ("metrics", "latency_ms"),
}


@router.get("/experiments", response_model=list[ExperimentRow], summary="PRD §74 Experiment 列表")
def list_experiments(
    workspace: WorkspaceDep,
    status: Annotated[str | None, Query()] = None,
) -> list[ExperimentRow]:
    out: list[ExperimentRow] = []
    for experiment in workspace.experiment_store().list():
        if status and experiment.status.value != status:
            continue
        out.append(
            ExperimentRow(
                id=experiment.id,
                name=experiment.name,
                benchmark_id=experiment.benchmark_id,
                status=experiment.status.value,
                variants=len(experiment.variants),
                created_at=experiment.created_at.isoformat() if experiment.created_at else None,
                dataset_version=experiment.dataset_version or None,
                profile=experiment.profile,
                repeat=experiment.repeat,
                gate=experiment.gate,
                note=experiment.note,
            )
        )
    return out


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentDetail,
    summary="PRD §74/§26–§28 variant 对比与按维度分组",
)
def experiment_detail(
    experiment_id: str,
    workspace: WorkspaceDep,
    dimension: Annotated[str | None, Query(description="按维度分组（model/prompt/...）")] = None,
) -> ExperimentDetail:
    try:
        experiment = workspace.experiment_store().load(experiment_id)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    store = workspace.run_store()
    rows = [_variant_row(workspace, store, variant) for variant in experiment.variants]
    dimensions = sorted({key for row in rows for key in row.dimensions})
    groups: dict[str, list[str]] = {}
    if dimension:
        from agent_eval.experiment.runner import VariantResult

        proxies = [
            VariantResult(
                variant_id=row.variant_id,
                name=row.name,
                dimensions=row.dimensions,
                run_id=row.run_id,
                status=row.status,
            )
            for row in rows
        ]
        groups = {
            key: [item.variant_id for item in items]
            for key, items in variant_matrix(proxies, dimension).items()
        }
    return ExperimentDetail(
        experiment=experiment.model_dump(mode="json"),
        variants=rows,
        dimensions=dimensions,
        groups=groups,
    )


def _variant_row(workspace: WorkspaceDep, store: RunStore, variant: Any) -> VariantRow:
    row = VariantRow(
        variant_id=variant.id,
        name=variant.name,
        dimensions=dict(variant.dimensions),
        status=variant.status,
        run_id=variant.run_id or None,
    )
    report = _report_for(workspace, store, variant.run_id)
    if report is None:
        return row
    metrics = report.get("metrics") or {}
    totals = report.get("totals") or {}
    row.verdict = report.get("verdict")
    for field, (section, key) in _METRIC_KEYS.items():
        source = metrics if section == "metrics" else totals
        value = source.get(key)
        if value is not None:
            setattr(row, field, value)
    cost = report.get("cost") or {}
    row.cost = cost.get("total_cost")
    row.flaky_cases = totals.get("flaky_cases")
    iterations = totals.get("iterations") or 0
    if iterations:
        row.stability = "STABLE" if not totals.get("flaky_cases") else "FLAKY"
    return row


def _report_for(workspace: WorkspaceDep, store: RunStore, run_id: str | None) -> dict | None:
    if not run_id:
        return None
    path = store.report_path(run_id, "report.json")
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


@router.get(
    "/experiments/{experiment_id}/comparison",
    response_model=list[dict],
    summary="PRD §26/§27/§28 模型 / 提示词 / Harness 对比",
)
def experiment_comparison(
    experiment_id: str,
    workspace: WorkspaceDep,
    dimension: Annotated[str, Query(description="对比维度")] = "model",
) -> list[dict[str, Any]]:
    detail = experiment_detail(experiment_id, workspace, dimension)
    by_id = {row.variant_id: row for row in detail.variants}
    out: list[dict[str, Any]] = []
    for group, variant_ids in detail.groups.items():
        members = [by_id[vid] for vid in variant_ids if vid in by_id]
        if not members:
            continue
        out.append(
            {
                "value": group,
                "variants": [row.variant_id for row in members],
                "runs": [row.run_id for row in members],
                "task_success": _mean([row.task_success for row in members]),
                "cost": _mean([row.cost for row in members]),
                "latency_ms": _mean([row.latency_ms for row in members]),
                "tokens": _mean([row.tokens for row in members]),
                "tool_calls": _mean([row.tool_calls for row in members]),
                "step_ratio": _mean([row.step_ratio for row in members]),
                "argument_checks": _mean([row.argument_checks for row in members]),
            }
        )
    out.sort(key=lambda row: (row["task_success"] is None, -(row["task_success"] or 0)))
    return out


def _mean(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return round(sum(known) / len(known), 6) if known else None


@router.get(
    "/experiments/{experiment_id}/runs",
    response_model=list[dict],
    summary="实验内各 variant 的 run（含未完成/失败）",
)
def experiment_runs(experiment_id: str, workspace: WorkspaceDep) -> list[dict[str, Any]]:
    try:
        experiment: Experiment = workspace.experiment_store().load(experiment_id)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return [
        {
            "variant_id": variant.id,
            "name": variant.name,
            "dimensions": variant.dimensions,
            "run_id": variant.run_id,
            "status": variant.status,
            "gate": variant.gate,
            "agent_endpoint": variant.agent_endpoint,
        }
        for variant in experiment.variants
    ]
