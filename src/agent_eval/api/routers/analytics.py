"""Trends / Cost / Flaky 只读接口（PRD §58/§59/§32）。

这三类视图全部依赖 DuckDB 派生层。投影未构建时**不返回空数组冒充"没有数据"**，
而是返回 ``projection="missing"`` + 一条可执行的 hint（Spec §1.3：派生层可重建，
但"未构建"与"构建后为空"是两种不同状态）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from agent_eval.api.deps import WorkspaceDep, missing_projection_hint
from agent_eval.api.schemas import CostBreakdown, FlakyList, TrendPoint, TrendSeries
from agent_eval.api.services import load_view
from agent_eval.errors import AgentEvalError

router = APIRouter(tags=["analytics"])


@router.get("/trends", response_model=TrendSeries, summary="PRD §58 Historical Trend")
def trends(
    workspace: WorkspaceDep,
    benchmark: Annotated[str | None, Query()] = None,
    dataset_version: Annotated[str | None, Query()] = None,
    metric: Annotated[str | None, Query(description="平台 metric id")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> TrendSeries:
    analytics = workspace.analytics()
    if analytics is None:
        return TrendSeries(
            projection="missing",
            hint=missing_projection_hint(str(workspace.analytics_path)),
            benchmark=benchmark,
            dataset_version=dataset_version,
            metric=metric,
        )
    try:
        rows = analytics.trend(benchmark, dataset_version, limit)
        metric_rows = analytics.metric_trend(metric, limit) if metric else []
    finally:
        analytics.close()
    return TrendSeries(
        projection="ok",
        benchmark=benchmark,
        dataset_version=dataset_version,
        metric=metric,
        points=[
            TrendPoint(
                run_id=row["run_id"],
                benchmark_id=row.get("benchmark_id"),
                agent_model=row.get("agent_model"),
                git_commit=row.get("git_commit"),
                dataset_version=row.get("dataset_version"),
                started_at=_iso(row.get("started_at")),
                verdict=row.get("verdict"),
                task_success=row.get("task_success"),
                total_cases=row.get("total_cases"),
                passed_cases=row.get("passed_cases"),
                failed_cases=row.get("failed_cases"),
                total_tokens=row.get("total_tokens"),
                total_cost=row.get("total_cost"),
            )
            for row in rows
        ],
        metric_points=[{**row, "started_at": _iso(row.get("started_at"))} for row in metric_rows],
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


@router.get("/cost", response_model=list[CostBreakdown], summary="PRD §59 Cost Analysis")
def cost(
    workspace: WorkspaceDep,
    benchmark: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[CostBreakdown]:
    metas = workspace.run_store().list_runs()
    metas.sort(key=lambda meta: meta.started_at, reverse=True)
    out: list[CostBreakdown] = []
    priced_any = False
    for meta in metas:
        if benchmark and meta.benchmark_id != benchmark:
            continue
        try:
            view = load_view(workspace.run_store(), meta.run_id, workspace.evals_root)
        except AgentEvalError:
            continue
        report = view.aggregate().cost
        priced_any = priced_any or bool(report.priced_cases)
        out.append(
            CostBreakdown(
                run_id=meta.run_id,
                priced_cases=report.priced_cases,
                unpriced_cases=report.unpriced_cases,
                price_configured=bool(report.priced_cases),
                total_cost=report.total_cost,
                agent_cost=report.agent_cost,
                judge_cost=report.judge_cost,
                avg_cost_per_case=report.avg_cost_per_case,
                cost_per_success=report.cost_per_success,
                judge_cost_ratio=report.judge_cost_ratio,
                by_model={meta.agent_model: report.total_cost}
                if meta.agent_model and report.total_cost is not None
                else {},
            )
        )
        if len(out) >= limit:
            break
    note = (
        None
        if priced_any
        else "evals/pricing.yaml 未配置可匹配的模型定价：cost 一律为 null（PRD §59 禁止伪造 0.0）"
    )
    if note:
        for row in out:
            row.note = note
    return out


@router.get("/flaky", response_model=FlakyList, summary="PRD §32 Flaky Detection（投影口径）")
def flaky(
    workspace: WorkspaceDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> FlakyList:
    analytics = workspace.analytics()
    if analytics is None:
        return FlakyList(
            projection="missing",
            hint=missing_projection_hint(str(workspace.analytics_path)),
        )
    try:
        rows = analytics.flaky_cases(limit)
    finally:
        analytics.close()
    return FlakyList(
        projection="ok",
        total=len(rows),
        cases=[
            {
                "run_id": row["run_id"],
                "case_id": row["case_id"],
                "iterations": row["iterations"],
                "passes": row["passes"],
                "fails": row["fails"],
                "errors": row["errors"],
            }
            for row in rows
        ],
    )


@router.get("/storage/status", response_model=dict, summary="派生层行数（Spec §1.3 可重建性）")
def storage_status(workspace: WorkspaceDep) -> dict[str, Any]:
    analytics = workspace.analytics()
    if analytics is None:
        return {
            "projection": "missing",
            "path": str(workspace.analytics_path),
            "hint": missing_projection_hint(str(workspace.analytics_path)),
            "counts": {},
        }
    try:
        counts = analytics.counts()
    finally:
        analytics.close()
    return {"projection": "ok", "path": str(workspace.analytics_path), "counts": counts}
