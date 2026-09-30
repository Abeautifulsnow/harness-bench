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
from agent_eval.models.run import RunMetadata, endpoint_kind
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


# 资源类 metric 的噪声下限（ROADMAP 发现的 1）：MetricDiff 的方向只在变化幅度
# 超出与 case 级性能回归（_case_performance）**同源**的阈值时才有信号——
# 阈值内判 unchanged。方向是信号不是算术：抖动 +11% 标 "regressed" 与抖动 -11%
# 标 "improved" 是同一种错误。
# 键：metric id 或其后缀 → DEFAULT_PERFORMANCE_THRESHOLDS 的键。cost 无阈值
# （定价是确定性的，没有 wall-clock 抖动），保持逐字方向。
_NOISE_FLOOR_SOURCES: tuple[tuple[str, str], ...] = (
    ("tool_calls", "tool_calls"),
    (".tool_calls", "tool_calls"),
    ("tokens", "tokens"),
    (".tokens", "tokens"),
    (".token_count", "tokens"),
    ("latency_ms", "latency_ms"),
    (".latency_ms", "latency_ms"),
    (".duration_ms", "latency_ms"),
)
# wall-clock 时长的信号门限（毫秒）：差值在时钟分辨率以内 = 抖动，任一侧低于分辨率
# = 那一侧根本不可测。两种都没有信号，两个都拦。
# 只按"两侧都在下限以下"拦会漏掉一半：0.4ms 的基线配上 5ms 的候选（最小值低于
# 分辨率，相对变化 ±1000%），以及恰好等于 1.0ms 的一侧（严格小于不成立，掉进相对
# 阈值分支）。后者是联调实测的间歇 flake —— 5 个 case 全 1ms 的那一侧均值正好
# 1.0ms，与 0.6ms 比出 ``improved``（-40% > 20% 阈值），
# ``test_api.py::test_regression_between_two_runs`` 于是断言失败。
_WALL_CLOCK_FLOOR = 1.0  # ms
_WALL_CLOCK_METRICS = frozenset({"latency_ms", ".latency_ms", ".duration_ms"})


def _noise_floor(metric_id: str, thresholds: dict[str, float]) -> float | None:
    """该 metric 的噪声下限（百分比）；None = 无下限，保持逐字方向。"""
    if metric_id in _WALL_CLOCK_METRICS:
        return thresholds.get("latency_ms")
    for suffix, key in _NOISE_FLOOR_SOURCES:
        if metric_id == suffix or metric_id.endswith(suffix):
            return thresholds.get(key)
    return None


def _is_wall_clock(metric_id: str) -> bool:
    return any(metric_id == name or metric_id.endswith(name) for name in _WALL_CLOCK_METRICS)


def _below_wall_clock_floor(metric_id: str, base: float, candidate: float) -> bool:
    """这一对 wall-clock 数值之间没有可判读的差异（PRD §55 性能维度不给方向）。

    两个条件任一成立即无信号：
    1. 差值在时钟分辨率以内 —— 时间戳的量化步长就是 1ms，1ms ↔ 2ms 的"翻倍"
       是量化不是耗时；
    2. 任一侧低于分辨率 —— 那一侧本来就不可测。基线 0.4ms 对候选 5ms 的相对
       变化是 ±1000%，相对阈值在近 0 基线上只会放大噪声（分母失义）。
    真实量级不受影响：300ms ↔ 400ms 两侧都可测、差值远超分辨率，照判退步。
    """
    if not _is_wall_clock(metric_id):
        return False
    return (
        abs(candidate - base) <= _WALL_CLOCK_FLOOR
        or min(base, candidate) < _WALL_CLOCK_FLOOR
    )


def _within_noise(
    metric_id: str, base: float, candidate: float, floor_percent: float | None
) -> bool:
    if _below_wall_clock_floor(metric_id, base, candidate):
        return True
    if base <= 0:
        return False  # 相对阈值需要正基线；0 基线的抖动交给绝对判据（上行）
    return abs(candidate - base) / base * 100 <= floor_percent


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
    if both and _below_wall_clock_floor(metric, baseline_value, candidate_value):
        # 毫秒以内的耗时差是时钟分辨率，不是退步：这一条会进
        # ``comparison.performance_regressions``（Gate 的输入），此前 case 级完全没有
        # 绝对下限——1ms ↔ 2ms 的抖动就足以判退步（-100%/+100%）。
        return PerformanceDiff(
            metric=metric,
            baseline=baseline_value,
            candidate=candidate_value,
            delta_percent=delta_percent,
            regressed=False,
            threshold_percent=threshold,
        )
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
    baseline_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
    performance_thresholds: dict[str, float] | None = None,
) -> list[MetricDiff]:
    """metric-level 段（PRD §53/§105）：只比对两侧都存在的 metric id。

    ``verdict`` 语义（方向敏感）：
    - 质量类 metric（task_success / native.* / agent.* …）：值上升为 ``improved``；
    - 成本与性能类 metric（tokens / tool_calls / latency_ms / cost / task_failure）：
      值上升为 ``regressed`` —— 用更多 token 跑出同样的成功率不是进步。
    - 资源类 metric 带噪声下限（与 case 级性能回归同源的阈值）：变化幅度在阈值内
      判 ``unchanged``——latency 是 wall-clock，调度抖动就足以让均值跨过 1e-9 的
      逐字比较，方向必须在有信号的幅度上才给（ROADMAP 发现的 1）。
    """
    thresholds = performance_thresholds or DEFAULT_PERFORMANCE_THRESHOLDS
    diffs: list[MetricDiff] = []
    for metric_id in sorted(set(baseline_metrics) & set(candidate_metrics)):
        base = baseline_metrics[metric_id]
        cand = candidate_metrics[metric_id]
        delta = round(cand - base, 6)
        delta_percent = round(delta / base * 100, 4) if base else None
        floor = _noise_floor(metric_id, thresholds)
        if abs(delta) < 1e-9 or (floor is not None and _within_noise(metric_id, base, cand, floor)):
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
    # 守卫链：每条"两侧不可比"的事实各追加一条原因。**不覆盖**（早先是 last-wins
    # 赋值）——一次 run 里同时踩中两条时，只报最后一条会让前一条原因彻底消失，
    # 而每条原因指向不同的修法（换 dataset / 换套件 / 换模型 / 换基线来源）。
    reasons: list[str] = []
    if baseline_meta.dataset_version != candidate_meta.dataset_version:
        # Spec §4.3：禁止静默跨 dataset_version 比较
        reasons.append(
            f"dataset_version mismatch: baseline={baseline_meta.dataset_version} "
            f"candidate={candidate_meta.dataset_version}（Spec §1.2-3 禁止跨版本比较）"
        )
    if baseline_meta.benchmark_id != candidate_meta.benchmark_id:
        reasons.append(
            f"benchmark mismatch: baseline={baseline_meta.benchmark_id} "
            f"candidate={candidate_meta.benchmark_id}"
        )
    if baseline_meta.suites_covered != candidate_meta.suites_covered:
        # ROADMAP「发现的 2」：run 级均值是"该 run 选中的 case 集合"上的均值，
        # 集合不同则数值不可比——`--suite smoke`（3 条）对 `--suite golden`（24 条）
        # 比平均工具调用数只会产出噪声回归。与"禁止跨 dataset_version 比较"同一条
        # 原则：没有可比性就明说（valid=False → Gate 退化绝对阈值），不静默给方向。
        reasons.append(
            f"suites_covered mismatch: baseline={baseline_meta.suites_covered or '{}'} "
            f"candidate={candidate_meta.suites_covered or '{}'}"
            "（不同 case 集合的均值不可比，Spec §4.3 禁止静默跨集合比较）"
        )
    if endpoint_kind(baseline_meta.agent_endpoint) != endpoint_kind(candidate_meta.agent_endpoint):
        # 联调实测（2026-09-30）：本机连续跑 fake 冒烟后，第一次接真实 SUT 的 run
        # 解析到 `fake://` 的 run 当基线，于是"native.output_checks 回归 -100%"这种
        # 纯由换 SUT 造成的数字被当成回归信号摆上台面。换 SUT 与换 dataset 同类：
        # 两次 run 的共同前提（同一个被测对象）不成立，比较没有价值前提。
        # 粒度是**接入类型**（fake / http(s)）而不是整条 URL：同一个 SUT 在开发机与
        # CI 上必然 host/port 不同，钉死 URL 会让跨机基线永远不可比；模型漂移由
        # agent_model 守卫覆盖。双侧都为空（旧 run 未记录 endpoint）不拦。
        reasons.append(
            f"agent endpoint kind mismatch: baseline={baseline_meta.agent_endpoint or '-'} "
            f"candidate={candidate_meta.agent_endpoint or '-'}"
            "（fake 内置 mock 与真实 SUT 之间没有可比性，Spec §4.3 禁止静默跨接入类型比较）"
        )
    if (baseline_meta.agent_model or None) != (candidate_meta.agent_model or None):
        # A4：模型漂移让 token / 延迟 / 通过率全变，报告却会归因为"回归"——
        # 这直接打在回归平台的核心主张上。agent_model 从此是受校验字段
        # （值来自 health 阶段 SUT 自报的实际生效模型），不是自由标签。
        # 双侧均为空（旧 run 未记录）无法证伪可比性，不拦。
        reasons.append(
            f"agent model mismatch: baseline={baseline_meta.agent_model or '-'} "
            f"candidate={candidate_meta.agent_model or '-'}"
            "（跨模型的 token/延迟/通过率不可比，Spec §4.3 禁止静默跨模型比较）"
        )
    if (baseline_meta.token_usage_scope or None) != (candidate_meta.token_usage_scope or None):
        # A3 修订五的同批守卫：只校输入侧的 run 与校全部用量的 run，
        # `tokens.max_regression_percent` 比出来的差值里混着口径变化——
        # "口径漂移被伪装成回归"正是 §4.3 要禁的"不可比"。双侧均空（旧 run
        # 未记录口径）与 suites_covered 同理放行。
        reasons.append(
            f"token usage scope mismatch: baseline={baseline_meta.token_usage_scope or '-'} "
            f"candidate={candidate_meta.token_usage_scope or '-'}"
            "（用量口径不同的 run 不可比：单侧观测的总量是被低估的，Spec §4.3）"
        )
    if reasons:
        comparison.valid = False
        comparison.invalid_reason = "；".join(reasons)

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
            case.metric_diffs = _metric_diffs(
                _case_metrics(base_iters), _case_metrics(cand_iters), thresholds
            )
        comparison.cases.append(case)

    _fill_counts(comparison)
    comparison.metrics = _metric_diffs(
        _run_metric_means(baseline_results), _run_metric_means(candidate_results), thresholds
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
