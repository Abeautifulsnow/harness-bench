"""Dashboard 只读接口（PRD §72 首页）。

首页语义：**先回答"当前发布是什么、还健康吗"**，而不是罗列所有数字。
因此 ``current_release`` 来自 release mode 的 baseline（Spec §4.5 的发布锚点），
指标卡来自最近一次 run 的 report.json；缺数据时字段为 None（不是 0），
让 UI 能区分"指标是 0"和"平台里还没有数据"。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from agent_eval.api.deps import WorkspaceDep
from agent_eval.api.schemas import Dashboard, DashboardCard
from agent_eval.api.services import load_view
from agent_eval.errors import AgentEvalError
from agent_eval.models.regression import BaselineMode

router = APIRouter(tags=["dashboard"])


@router.get("/health", summary="存活探针（不检查数据）")
def health(request: Request) -> dict[str, Any]:
    workspace = request.app.state.workspace
    runs = workspace.run_store().list_runs()
    projection = "ok" if workspace.analytics_path.is_file() else "missing"
    return {
        "status": "ok",
        "version": request.app.state.version,
        "evals_root": str(workspace.evals_root),
        "data_root": str(workspace.data_root),
        "projection": projection,
        "runs": len(runs),
    }


@router.get("/dashboard", response_model=Dashboard, summary="PRD §72 Current Release + 核心卡片")
def dashboard(
    workspace: WorkspaceDep,
    benchmark: Annotated[str | None, Query()] = None,
) -> Dashboard:
    metas = workspace.run_store().list_runs()
    metas.sort(key=lambda meta: meta.started_at, reverse=True)
    if benchmark:
        metas = [meta for meta in metas if meta.benchmark_id == benchmark]

    release_baseline = None
    for candidate in workspace.baseline_store().effective():
        if candidate.mode == BaselineMode.release and candidate.pinned_run_id:
            if benchmark and candidate.benchmark_id != benchmark:
                continue
            release_baseline = candidate
            break

    cards: list[DashboardCard] = []
    recent = metas[:20]
    summary = _summary(workspace, metas[:1])
    if summary is not None:
        totals, metrics, cost = summary
        cards.extend(
            [
                DashboardCard(
                    label="Task Success",
                    value=metrics.get("task_success"),
                    unit="ratio",
                    detail=(
                        f"{totals.get('passed_iterations')}/{totals.get('iterations')} iterations"
                    ),
                ),
                DashboardCard(
                    label="Regression Count",
                    value=totals.get("regression_cases"),
                    unit="cases",
                    detail=f"baseline_mode={metas[0].baseline_mode}",
                ),
                DashboardCard(
                    label="Security Failures",
                    value=_security_failures(workspace, metas[0].run_id),
                    unit="findings",
                ),
                DashboardCard(label="Avg Cost", value=cost.get("total_cost"), unit="USD"),
                DashboardCard(
                    label="Avg Latency",
                    value=metrics.get("latency_ms"),
                    unit="ms",
                    detail="per iteration",
                ),
                DashboardCard(
                    label="Flaky Cases",
                    value=totals.get("flaky_cases"),
                    unit="cases",
                ),
            ]
        )
    return Dashboard(
        current_release=release_baseline.pinned_run_id if release_baseline else None,
        release_run_id=release_baseline.pinned_run_id if release_baseline else None,
        cards=cards,
        recent_runs=recent,
        projection="ok" if workspace.analytics_path.is_file() else "missing",
        hint=(
            None
            if workspace.analytics_path.is_file()
            else "派生层未构建：`agent-eval storage rebuild` 后趋势/聚类视图可用；"
            "首页与 run 详情读事实层，不受影响。"
        ),
    )


def _summary(workspace: WorkspaceDep, metas: list[Any]):
    if not metas:
        return None
    try:
        view = load_view(workspace.run_store(), metas[0].run_id, workspace.evals_root)
    except AgentEvalError:
        return None
    aggregate = view.aggregate()
    return aggregate.counts, aggregate.metrics, aggregate.cost.as_dict()


def _security_failures(workspace: WorkspaceDep, run_id: str) -> int:
    try:
        view = load_view(workspace.run_store(), run_id, workspace.evals_root)
    except AgentEvalError:
        return 0
    return sum(
        1
        for result in view.results
        for metric in result.all_metric_results
        if metric.metric.startswith("security.") and metric.verdict in {"fail", "error"}
    )
