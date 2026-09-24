"""Regression 只读接口（PRD §53–§57/§77/§105）。

比较是**派生关系**：不落盘、按需重算。两侧 dataset_version / benchmark 不一致时
返回 ``valid=false`` 且禁止给出 REGRESSION（Spec §1.2-3）——API 透传这一语义，
不把它"修好"成看起来正常的对比。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from agent_eval.api.deps import WorkspaceDep
from agent_eval.api.schemas import RegressionAnalysis
from agent_eval.api.services import comparison_pairs
from agent_eval.errors import AgentEvalError
from agent_eval.models.regression import RegressionComparison
from agent_eval.regression.compare import compare_runs
from agent_eval.storage.run_store import RunStore

router = APIRouter(tags=["regression"])


def _payload(comparison: RegressionComparison) -> RegressionAnalysis:
    """RegressionComparison → 视图模型（性能项从每个 CaseDiff 里提上来汇总）。

    ``PerformanceDiff`` 挂在 CaseDiff 上（case 级），Run 级视图需要的是"哪些 case
    的性能退步了"，因此这里同时展开明细与 ``performance_regressions`` 摘要。
    """
    performance = [
        {"case_id": case.case_id, **diff.model_dump(mode="json")}
        for case in comparison.cases
        for diff in case.performance
    ]
    return RegressionAnalysis(
        run_id=comparison.candidate_run_id,
        candidate_run_id=comparison.candidate_run_id,
        baseline_run_id=comparison.baseline_run_id,
        baseline_mode=comparison.baseline_mode,
        valid=comparison.valid,
        invalid_reason=comparison.invalid_reason,
        counts=comparison.counts,
        metric_diffs=[diff.model_dump(mode="json") for diff in comparison.metrics],
        performance=performance,
        flaky_cases=list(comparison.flaky_cases),
        failure_categories=[diff.model_dump(mode="json") for diff in comparison.failure_categories],
        cases=[
            {
                **case.model_dump(mode="json"),
                "state": case.state.value,
                "baseline_stability": case.baseline_stability.value,
                "candidate_stability": case.candidate_stability.value,
            }
            for case in comparison.cases
        ],
        trace_diffs=[diff.model_dump(mode="json") for diff in comparison.trace_diffs],
        baseline_totals=comparison.baseline_totals,
        candidate_totals=comparison.candidate_totals,
    )


def _compare(
    store: RunStore, baseline_run: str, candidate_run: str, with_traces: bool
) -> RegressionAnalysis:
    try:
        base_meta, base_results = store.load_run(baseline_run)
        cand_meta, cand_results = store.load_run(candidate_run)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    comparison = compare_runs(
        base_meta,
        base_results,
        cand_meta,
        cand_results,
        trace_loader=(
            _trace_loader(store, base_meta.run_id, cand_meta.run_id) if with_traces else None
        ),
    )
    return _payload(comparison)


def _trace_loader(store: RunStore, baseline_run_id: str, candidate_run_id: str):
    """两侧 trace 的 span 树加载器（PRD §56 trace diff 的输入）。"""
    from agent_eval.storage.run_store import _safe
    from agent_eval.trace.builder import TraceBuilder

    def loader(case_run):
        out: dict[str, list] = {}
        for side, run_id in (("baseline", baseline_run_id), ("candidate", candidate_run_id)):
            key = f"{_safe(case_run.case_id)}.iter{case_run.iteration}"
            events = store.load_events(run_id, key)
            if not events:
                out[side] = []
                continue
            builder = TraceBuilder()
            builder.feed_all(events)
            out[side] = builder.build().spans
        return out

    return loader


@router.get("/regressions", response_model=list[dict], summary="PRD §77 全部 baseline×candidate 对")
def list_regressions(
    workspace: WorkspaceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    return comparison_pairs(workspace.run_store(), limit)


@router.get(
    "/regressions/{run_id}",
    response_model=RegressionAnalysis,
    summary="用 run 自身记录的 baseline 做比较（PRD §105 十二项输出）",
)
def regression_of_run(
    run_id: str,
    workspace: WorkspaceDep,
    trace_diff: Annotated[bool, Query(description="是否计算 trace/argument diff")] = True,
) -> RegressionAnalysis:
    store = workspace.run_store()
    try:
        meta, _ = store.load_run(run_id)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    if not meta.baseline_run_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run {run_id} 没有 baseline（mode={meta.baseline_mode}；"
                f"{meta.baseline_reason or '未解析到合格历史 run'}）："
                "Spec §4.3 下所有回归判定为 UNDETERMINED，不存在可展示的对比"
            ),
        )
    return _compare(store, meta.baseline_run_id, run_id, trace_diff)


@router.get(
    "/regressions/{baseline_run}/{candidate_run}",
    response_model=RegressionAnalysis,
    summary="显式指定两侧 run 做比较（PRD §77 左右对比）",
)
def regression_between(
    baseline_run: str,
    candidate_run: str,
    workspace: WorkspaceDep,
    trace_diff: Annotated[bool, Query()] = True,
) -> RegressionAnalysis:
    return _compare(workspace.run_store(), baseline_run, candidate_run, trace_diff)
