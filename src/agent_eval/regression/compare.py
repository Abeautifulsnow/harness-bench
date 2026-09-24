"""Regression Compare（PRD §53 四层级 + §54 判定 + §55 性能回归 + §105 输出项）。

判定表以 Spec V2.1.1 §3.3 为准；两侧 dataset_version / case_version 必须一致
（Spec §1.2-3），不一致即 INVALID，禁止产出 REGRESSION。
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable

from agent_eval.models.regression import (
    BaselineMode,
    CaseDiff,
    FailureCategoryDiff,
    MetricDiff,
    PerformanceDiff,
    RegressionComparison,
    TraceDiff,
)
from agent_eval.models.results import (
    CaseRunResult,
    CaseStatus,
    RegressionState,
    Stability,
)
from agent_eval.models.run import RunMetadata
from agent_eval.regression.stability import compute_stability, regression_state

# PRD §55 的默认性能阈值（%），可被 Gate 规则覆盖
DEFAULT_PERFORMANCE_THRESHOLDS: dict[str, float] = {
    "tool_calls": 20.0,
    "tokens": 25.0,
    "latency_ms": 20.0,
}

_TRACE_DIFF_CAP = 50  # 单次 compare 最多产出多少条 trace diff（报告体量约束）

# 「越低越好」的 metric：数值上升 = 退步（PRD §55 性能维度 + 成本维度）。
# 其余 metric 一律「越高越好」（task_success / native.* / agent.* / harness.* 都是得分）。
_LOWER_IS_BETTER: frozenset[str] = frozenset(
    {"tool_calls", "tokens", "latency_ms", "cost", "task_failure"}
)
_LOWER_IS_BETTER_SUFFIXES: tuple[str, ...] = (
    ".tool_calls",
    ".tokens",
    ".latency_ms",
    ".cost",
    ".token_count",
    ".duration_ms",
)


def _lower_is_better(metric_id: str) -> bool:
    return metric_id in _LOWER_IS_BETTER or metric_id.endswith(_LOWER_IS_BETTER_SUFFIXES)


def _iterations(results: list[CaseRunResult], case_id: str) -> list[CaseRunResult]:
    return sorted([r for r in results if r.case_id == case_id], key=lambda r: r.iteration)


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 6) if values else None


def _performance_diff(
    metric: str,
    baseline_value: float | None,
    candidate_value: float | None,
    threshold: float | None,
) -> PerformanceDiff:
    delta_percent: float | None = None
    regressed = False
    both = baseline_value is not None and candidate_value is not None
    if both and baseline_value > 0:
        delta_percent = round((candidate_value - baseline_value) / baseline_value * 100, 4)
        regressed = threshold is not None and delta_percent > threshold
    return PerformanceDiff(
        metric=metric,
        baseline=baseline_value,
        candidate=candidate_value,
        delta_percent=delta_percent,
        regressed=regressed,
        threshold_percent=threshold,
    )


def _metric_diffs(
    baseline_metrics: dict[str, float], candidate_metrics: dict[str, float]
) -> list[MetricDiff]:
    """metric-level 段（PRD §53/§105）：只比对两侧都存在的 metric id。

    ``verdict`` 语义（方向敏感）：
    - 质量类 metric（task_success / native.* / agent.* …）：值上升为 ``improved``；
    - 成本与性能类 metric（tokens / tool_calls / latency_ms / cost / task_failure）：
      值上升为 ``regressed`` —— 用更多 token 跑出同样的成功率不是进步。
    """
    diffs: list[MetricDiff] = []
    for metric_id in sorted(set(baseline_metrics) & set(candidate_metrics)):
        base = baseline_metrics[metric_id]
        cand = candidate_metrics[metric_id]
        delta = round(cand - base, 6)
        delta_percent = round(delta / base * 100, 4) if base else None
        if abs(delta) < 1e-9:
            verdict = "unchanged"
        else:
            better = delta < 0 if _lower_is_better(metric_id) else delta > 0
            verdict = "improved" if better else "regressed"
        diffs.append(
            MetricDiff(
                metric=metric_id,
                baseline=base,
                candidate=cand,
                delta=delta,
                delta_percent=delta_percent,
                verdict=verdict,
            )
        )
    return diffs


def _case_performance(
    baseline_iters: list[CaseRunResult],
    candidate_iters: list[CaseRunResult],
    thresholds: dict[str, float],
) -> list[PerformanceDiff]:
    def agg(
        iterations: list[CaseRunResult], selector: Callable[[CaseRunResult], float]
    ) -> float | None:
        return _mean([selector(r) for r in iterations])

    pairs = {
        "tool_calls": (lambda r: float(len(r.tool_calls))),
        "tokens": (lambda r: float(r.token_count)),
        "latency_ms": (lambda r: float(r.latency_ms)),
    }
    return [
        _performance_diff(
            metric,
            agg(baseline_iters, selector),
            agg(candidate_iters, selector),
            thresholds.get(metric),
        )
        for metric, selector in pairs.items()
    ]


def _case_state(
    baseline_iters: list[CaseRunResult],
    candidate_iters: list[CaseRunResult],
) -> tuple[RegressionState, str | None, Stability, Stability]:
    """Spec §3.3 判定表 + 版本一致性前置校验（§1.2-3）。"""
    base_stability = compute_stability(baseline_iters[0].case_id, baseline_iters)
    cand_stability = compute_stability(candidate_iters[0].case_id, candidate_iters)
    base_version = {r.case_version for r in baseline_iters}
    cand_version = {r.case_version for r in candidate_iters}
    if base_version != cand_version:
        return (
            RegressionState.INVALID,
            f"case_version mismatch: baseline={sorted(base_version)} "
            f"candidate={sorted(cand_version)}",
            base_stability.stability,
            cand_stability.stability,
        )
    invalid = all(r.status == CaseStatus.ERROR for r in candidate_iters)
    state = regression_state(
        base_stability.stability,
        cand_stability.stability,
        candidate_invalid=invalid,
    )
    return RegressionState(state), None, base_stability.stability, cand_stability.stability


def _failure_category_diffs(
    baseline_results: list[CaseRunResult], candidate_results: list[CaseRunResult]
) -> list[FailureCategoryDiff]:
    def counts(results: list[CaseRunResult]) -> Counter[str]:
        return Counter(
            r.failure_category or "unclassified" for r in results if r.status == CaseStatus.FAIL
        )

    base = counts(baseline_results)
    cand = counts(candidate_results)
    return [
        FailureCategoryDiff(
            category=category,
            baseline=base.get(category, 0),
            candidate=cand.get(category, 0),
            delta=cand.get(category, 0) - base.get(category, 0),
        )
        for category in sorted(set(base) | set(cand))
    ]


def compare_runs(
    baseline_meta: RunMetadata,
    baseline_results: list[CaseRunResult],
    candidate_meta: RunMetadata,
    candidate_results: list[CaseRunResult],
    *,
    performance_thresholds: dict[str, float] | None = None,
    trace_loader: Callable[[CaseRunResult], dict[str, list]] | None = None,
    max_trace_diffs: int = _TRACE_DIFF_CAP,
) -> RegressionComparison:
    """Baseline × Candidate → PRD §105 的 12 项比较输出。

    ``trace_loader(case_run)`` 返回 ``{"baseline": spans, "candidate": spans}``（可选）。
    """
    thresholds = performance_thresholds or DEFAULT_PERFORMANCE_THRESHOLDS
    by_baseline = {
        cid: _iterations(baseline_results, cid) for cid in {r.case_id for r in baseline_results}
    }
    by_candidate = {
        cid: _iterations(candidate_results, cid) for cid in {r.case_id for r in candidate_results}
    }

    comparison = RegressionComparison(
        baseline_run_id=baseline_meta.run_id,
        candidate_run_id=candidate_meta.run_id,
        benchmark_id=candidate_meta.benchmark_id,
        dataset_id=candidate_meta.dataset_id,
        dataset_version=candidate_meta.dataset_version,
        baseline_dataset_version=baseline_meta.dataset_version,
        baseline_mode=baseline_meta.baseline_mode or BaselineMode.no_baseline.value,
    )
    if baseline_meta.dataset_version != candidate_meta.dataset_version:
        # Spec §4.3：禁止静默跨 dataset_version 比较
        comparison.valid = False
        comparison.invalid_reason = (
            f"dataset_version mismatch: baseline={baseline_meta.dataset_version} "
            f"candidate={candidate_meta.dataset_version}（Spec §1.2-3 禁止跨版本比较）"
        )
    if baseline_meta.benchmark_id != candidate_meta.benchmark_id:
        comparison.valid = False
        comparison.invalid_reason = (
            f"benchmark mismatch: baseline={baseline_meta.benchmark_id} "
            f"candidate={candidate_meta.benchmark_id}"
        )

    shared = sorted(set(by_baseline) & set(by_candidate))
    for case_id in sorted(set(by_baseline) | set(by_candidate)):
        base_iters = by_baseline.get(case_id, [])
        cand_iters = by_candidate.get(case_id, [])
        if not base_iters or not cand_iters:
            missing = "baseline" if not base_iters else "candidate"
            comparison.cases.append(
                CaseDiff(
                    case_id=case_id,
                    baseline_iterations=len(base_iters),
                    candidate_iterations=len(cand_iters),
                    invalid_reason=f"not present in {missing} run",
                )
            )
            continue
        state, invalid_reason, base_stability, cand_stability = _case_state(base_iters, cand_iters)
        case = CaseDiff(
            case_id=case_id,
            case_version=cand_iters[0].case_version,
            baseline_stability=base_stability,
            candidate_stability=cand_stability,
            state=state,
            invalid_reason=invalid_reason,
            performance=_case_performance(base_iters, cand_iters, thresholds),
            baseline_iterations=len(base_iters),
            candidate_iterations=len(cand_iters),
        )
        if state in {RegressionState.REGRESSION, RegressionState.IMPROVED}:
            case.metric_diffs = _metric_diffs(_case_metrics(base_iters), _case_metrics(cand_iters))
        comparison.cases.append(case)

    _fill_counts(comparison)
    comparison.metrics = _metric_diffs(
        _run_metric_means(baseline_results), _run_metric_means(candidate_results)
    )
    comparison.baseline_totals = _run_metric_means(baseline_results)
    comparison.candidate_totals = _run_metric_means(candidate_results)
    comparison.failure_categories = _failure_category_diffs(baseline_results, candidate_results)

    if trace_loader is not None:
        for case_id in shared:
            if len(comparison.trace_diffs) >= max_trace_diffs:
                break
            case_diff = next(c for c in comparison.cases if c.case_id == case_id)
            if case_diff.state not in {
                RegressionState.REGRESSION,
                RegressionState.IMPROVED,
                RegressionState.FLAKY,
            }:
                continue
            base_iter = by_baseline[case_id][0]
            cand_iter = by_candidate[case_id][0]
            spans = trace_loader(cand_iter) or {}
            comparison.trace_diffs.append(
                diff_for_case(
                    base_iter,
                    cand_iter,
                    baseline_spans=(spans.get("baseline") if spans else None) or [],
                    candidate_spans=(spans.get("candidate") if spans else None) or [],
                )
            )
    return comparison


def diff_for_case(
    baseline: CaseRunResult,
    candidate: CaseRunResult,
    *,
    baseline_spans: list | None = None,
    candidate_spans: list | None = None,
) -> TraceDiff:
    from agent_eval.regression.trace_diff import diff_case_runs

    return diff_case_runs(
        baseline,
        candidate,
        baseline_spans=baseline_spans,
        candidate_spans=candidate_spans,
    )


def _case_metrics(iterations: list[CaseRunResult]) -> dict[str, float]:
    out: dict[str, float] = {
        "tool_calls": float(_mean([len(r.tool_calls) for r in iterations]) or 0.0),
        "tokens": float(_mean([r.token_count for r in iterations]) or 0.0),
        "latency_ms": float(_mean([r.latency_ms for r in iterations]) or 0.0),
    }
    metric_ids = sorted({m.metric for r in iterations for m in r.all_metric_results})
    for metric_id in metric_ids:
        mean = _mean(
            [
                float(m.score)
                for r in iterations
                for m in r.all_metric_results
                if m.metric == metric_id and m.score is not None
            ]
        )
        if mean is not None:
            out[metric_id] = mean
    return out


def _run_metric_means(results: list[CaseRunResult]) -> dict[str, float]:
    if not results:
        return {}
    valid = [r for r in results if r.status in {CaseStatus.PASS, CaseStatus.FAIL}]
    out: dict[str, float] = {
        "task_success": round(len(valid) / len(results), 6),
        "tool_calls": float(_mean([len(r.tool_calls) for r in results]) or 0.0),
        "tokens": float(_mean([r.token_count for r in results]) or 0.0),
        "latency_ms": float(_mean([r.latency_ms for r in results]) or 0.0),
        "cost": float(_mean([r.cost for r in results]) or 0.0),
    }
    metric_ids = sorted({m.metric for r in results for m in r.all_metric_results})
    for metric_id in metric_ids:
        mean = _mean(
            [
                float(m.score)
                for r in results
                for m in r.all_metric_results
                if m.metric == metric_id and m.score is not None
            ]
        )
        if mean is not None:
            out[metric_id] = mean
    return out


def _fill_counts(comparison: RegressionComparison) -> None:
    states = Counter(case.state.value for case in comparison.cases)
    comparison.counts = {
        "cases": len(comparison.cases),
        "regression": states.get(RegressionState.REGRESSION.value, 0),
        "improved": states.get(RegressionState.IMPROVED.value, 0),
        "unchanged": states.get(RegressionState.UNCHANGED.value, 0),
        "flaky": states.get(RegressionState.FLAKY.value, 0),
        "invalid": states.get(RegressionState.INVALID.value, 0),
        "undetermined": states.get(RegressionState.UNDETERMINED.value, 0),
    }
    comparison.flaky_cases = [
        case.case_id for case in comparison.cases if case.state == RegressionState.FLAKY
    ]
    comparison.performance_regressions = [
        case.case_id
        for case in comparison.cases
        if any(diff.regressed for diff in case.performance)
    ]
