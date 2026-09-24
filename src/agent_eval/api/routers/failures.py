"""Failure Intelligence 只读接口（PRD §47–§51/§78/§107）。

分类与聚类都是**规则引擎的确定性输出**（PRD §48）：同一个 run 重复请求得到同一结果，
不依赖 LLM。`By Model / By Version / By Benchmark` 视角是同一批 failure 的不同切片。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from agent_eval.api.deps import WorkspaceDep
from agent_eval.api.schemas import ClusterList, ClusterRow, FailureList, FailureRow, TaxonomyRow
from agent_eval.errors import AgentEvalError
from agent_eval.failures.taxonomy import CATEGORIES, SUBCATEGORIES, parent_of
from agent_eval.storage.run_store import RunStore

router = APIRouter(tags=["failures"])


def _store(workspace: WorkspaceDep) -> RunStore:
    return workspace.run_store()


def _view(run_id: str, store: RunStore, evals_root):
    from agent_eval.api.services import load_view

    try:
        return load_view(store, run_id, evals_root)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.get("/failures/taxonomy", response_model=list[TaxonomyRow], summary="PRD §47 分类表")
def taxonomy() -> list[TaxonomyRow]:
    return [
        TaxonomyRow(category=category, parent=parent_of(category), description=description)
        for category, description in sorted(SUBCATEGORIES.items())
    ]


@router.get("/failures", response_model=list[FailureList], summary="跨 run 的 failure 列表")
def list_all_failures(
    workspace: WorkspaceDep,
    benchmark: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[FailureList]:
    out: list[FailureList] = []
    metas = workspace.run_store().list_runs()
    metas.sort(key=lambda meta: meta.started_at, reverse=True)
    for meta in metas:
        if benchmark and meta.benchmark_id != benchmark:
            continue
        view = _view(meta.run_id, workspace.run_store(), workspace.evals_root)
        payload = _failure_list(view, category)
        if not payload.failures:
            continue
        out.append(payload)
        if len(out) >= limit:
            break
    return out


@router.get("/failures/{run_id}", response_model=FailureList, summary="PRD §78 六种视角切片")
def run_failures(
    run_id: str,
    workspace: WorkspaceDep,
    category: Annotated[str | None, Query(description="二级分类")] = None,
    parent: Annotated[str | None, Query(description=" | ".join(CATEGORIES))] = None,
) -> FailureList:
    view = _view(run_id, workspace.run_store(), workspace.evals_root)
    rows = [
        failure
        for failure in view.analysis().failures
        if (category is None or failure["category"] == category)
        and (parent is None or failure["parent"] == parent)
    ]
    return _failure_list(view, None, rows)


def _failure_list(view, category: str | None, rows: list | None = None) -> FailureList:
    analysis = view.analysis()
    failures = rows if rows is not None else analysis.failures
    if category:
        failures = [f for f in failures if f["category"] == category]
    by_category: dict[str, int] = {}
    by_parent: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    for failure in failures:
        by_category[failure["category"]] = by_category.get(failure["category"], 0) + 1
        by_parent[failure["parent"]] = by_parent.get(failure["parent"], 0) + 1
        for tool in failure.get("tools") or []:
            by_tool[tool] = by_tool.get(tool, 0) + 1
    return FailureList(
        projection="ok",
        run_id=view.meta.run_id,
        total=len(failures),
        by_category=by_category,
        by_parent=by_parent,
        by_tool=by_tool,
        by_model=view.by_model(),
        by_version=view.by_version(),
        by_benchmark=view.by_benchmark(),
        failures=[
            FailureRow(
                failure_id=failure["id"],
                case_run_id=failure["case_run_id"],
                case_id=failure["case_id"],
                iteration=failure["iteration"],
                category=failure["category"],
                parent=failure["parent"],
                metric=failure.get("metric"),
                reason=failure.get("reason"),
                evidence=failure.get("evidence"),
                source=failure.get("source", "rule"),
                tags=[],
            )
            for failure in failures
        ],
    )


@router.get(
    "/failures/{run_id}/clusters",
    response_model=ClusterList,
    summary="PRD §49/§50 Failure Clustering（含代表 Case）",
)
def run_clusters(run_id: str, workspace: WorkspaceDep) -> ClusterList:
    view = _view(run_id, workspace.run_store(), workspace.evals_root)
    analysis = view.analysis()
    return ClusterList(
        projection="ok",
        run_id=run_id,
        total_clusters=len(analysis.clusters),
        total_failures=len(analysis.failures),
        clusters=[
            ClusterRow(
                cluster_id=cluster.cluster_id,
                label=cluster.label,
                category=cluster.category,
                parent=cluster.parent,
                size=cluster.size,
                case_ids=list(cluster.case_ids),
                representative_case_id=cluster.representative_case_id,
                representative_case_run_id=cluster.representative_case_run_id,
                common_tool_sequence=cluster.common_tool_sequence or "",
                common_error=cluster.common_error or None,
                first_seen=cluster.first_seen,
                latest_seen=cluster.latest_seen,
                affected_versions=[str(v) for v in cluster.affected_versions],
            )
            for cluster in analysis.clusters
        ],
    )


@router.get("/drafts", response_model=list[dict], summary="PRD §51 Promote 产生的 Case Draft")
def list_drafts(
    workspace: WorkspaceDep,
    suite: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query(description="draft | reviewed | active")] = None,
) -> list[dict]:
    return workspace.draft_store().list(suite=suite, status=status)
