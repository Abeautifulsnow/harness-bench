"""Run aggregation: the single source for every run artifact (Spec §6.2/§6.3).

report.json / gate.json / junit.xml / report.html / summary.md are rendered from
ONE ``RunAggregate`` instance in one write pass — the spec forbids deriving the
junit failure/error counts from a separate path than gate.json.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from agent_eval.models.regression import BaselineMode, GateReport, RegressionComparison
from agent_eval.models.results import (
    CaseRunResult,
    CaseStability,
    CaseStatus,
    MetricResultModel,
    RegressionState,
    Stability,
)
from agent_eval.models.run import FailureSemantics, RunMetadata, RunStatus
from agent_eval.regression.stability import compute_stability
from agent_eval.reports.cost import CostReport, compute_cost_report

PERFORMANCE_METRICS = ("tool_calls", "tokens", "latency_ms", "cost")


@dataclass
class CaseAggregate:
    case_id: str
    case_version: int = 1
    tags: list[str] = field(default_factory=list)
    iterations: int = 0
    pass_rate: float = 0.0
    stability: Stability = Stability.UNKNOWN
    regression_state: RegressionState = RegressionState.UNDETERMINED
    valid_iterations: int = 0
    infra_error_count: int = 0
    tool_calls_mean: float = 0.0
    tokens_mean: float = 0.0
    latency_mean: float = 0.0
    cost_mean: float = 0.0
    score_mean: float | None = None
    score_stddev: float | None = None
    latency_cv: float | None = None
    token_variance: float | None = None
    tool_sequence_variance: float | None = None
    pass_at_k: dict[str, float] = field(default_factory=dict)
    metric_means: dict[str, float] = field(default_factory=dict)
    blocking_failures: list[dict[str, Any]] = field(default_factory=list)
    error_semantics: list[str] = field(default_factory=list)
    failure_category: str | None = None
    # Spec §19.1.1：`skipped` 是独立结局。全部 metric 都 skipped 的 case 是
    # "什么都没判成"，不是"判过了"——junit 必须能把它渲染成 skipped（§6.3），
    # 否则观测不足的 case 会以 passed 的形态出现在 CI 报告里。
    evaluated_metrics: int = 0
    # case 级产物指针（PRD §90，ROADMAP 发现的 4）：只有 iteration/name/kind/
    # path/bytes，不塞内容——内容走 REST 端点按需读。report.json / report.html /
    # summary.md 由此让"只拿报告的 CI 读者"能找到失败现场。指针从各 iteration
    # 的 CaseRunResult.artifacts 摊平而来；iteration 必须进指针——Spec §21.3
    # 刻意不把它放进 ArtifactRecord 是因为宿主对象给得出，摊平到 case 级后宿主
    # 是多个 iteration，不标 iteration 就无法与下载端点的查名参数对上。
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_error(self) -> bool:
        return bool(self.error_semantics)

    @property
    def is_unjudged(self) -> bool:
        """没有一条 metric 真正参与判定（全部 skipped 或根本没产出）。"""
        return self.evaluated_metrics == 0

    def as_dict(self) -> dict[str, Any]:
        """Read-model shape shared by report.json and the REST API (PRD §75 Cases tab)."""
        return {
            "case_id": self.case_id,
            "case_version": self.case_version,
            "tags": list(self.tags),
            "iterations": self.iterations,
            "valid_iterations": self.valid_iterations,
            "pass_rate": self.pass_rate,
            "stability": self.stability.value,
            "regression_state": self.regression_state.value,
            "infra_error_count": self.infra_error_count,
            "failure_semantics": list(self.error_semantics),
            "failure_category": self.failure_category,
            "tool_calls_mean": self.tool_calls_mean,
            "tokens_mean": self.tokens_mean,
            "latency_mean": self.latency_mean,
            "cost_mean": self.cost_mean,
            "score_mean": self.score_mean,
            "score_stddev": self.score_stddev,
            "latency_cv": self.latency_cv,
            "token_variance": self.token_variance,
            "tool_sequence_variance": self.tool_sequence_variance,
            "pass_at_k": dict(self.pass_at_k),
            "metric_means": dict(self.metric_means),
            "blocking_failures": list(self.blocking_failures),
            "evaluated_metrics": self.evaluated_metrics,
            "artifacts": [dict(item) for item in self.artifacts],
        }


@dataclass
class RunAggregate:
    run: RunMetadata
    cases: list[CaseAggregate]
    counts: dict[str, int]
    metrics: dict[str, float]
    verdict: str  # pass | fail | infra_failure
    comparison: RegressionComparison | None = None
    gate: GateReport | None = None
    cost: CostReport = field(default_factory=CostReport)
    failures: list[Any] = field(default_factory=list)  # P3 failure analysis 挂载点
    clusters: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def baseline_mode(self) -> str:
        return self.run.baseline_mode or BaselineMode.no_baseline.value


def metric_mean(results: list[MetricResultModel], metric: str) -> float | None:
    scores = [m.score for m in results if m.metric == metric and m.score is not None]
    return round(statistics.mean(scores), 6) if scores else None


def run_metrics(results: list[CaseRunResult]) -> dict[str, float]:
    """Run-level numbers (PRD §81 + §105): always CaseRun aggregations, never facts."""
    if not results:
        return {}
    valid = [r for r in results if r.status in {CaseStatus.PASS, CaseStatus.FAIL}]
    failure_results = [r for r in results if r.status == CaseStatus.FAIL]
    priced = [r for r in results if r.cost_known]
    out: dict[str, float] = {
        "task_success": round(len(valid) / len(results), 6),
        "task_failure": round(len(failure_results) / len(results), 6),
        "tool_calls": round(statistics.mean(len(r.tool_calls) for r in results), 6),
        "tokens": round(statistics.mean(r.token_count for r in results), 6),
        "latency_ms": round(statistics.mean(r.latency_ms for r in results), 6),
    }
    if priced:
        # 只有存在定价时才有 cost 这一项；无定价时整项缺失（而非 0.0）
        out["cost"] = round(statistics.mean(r.cost for r in priced), 10)
    all_metrics = [m for r in results for m in r.all_metric_results]
    for metric_id in sorted({m.metric for m in all_metrics}):
        mean = metric_mean(all_metrics, metric_id)
        if mean is not None:
            out[metric_id] = mean
    return out


def _case_aggregate(
    case_id: str,
    iterations: list[CaseRunResult],
    stability: CaseStability,
    regression_state: RegressionState,
) -> CaseAggregate:
    valid = [r for r in iterations if r.status in {CaseStatus.PASS, CaseStatus.FAIL}]
    all_metrics = [m for r in iterations for m in r.all_metric_results]
    blocking_failures = [
        {
            "iteration": r.iteration,
            "case_run_id": r.id,
            "metric": m.metric,
            "mount": m.metadata.get("mount"),
            "turn": m.metadata.get("turn"),
            "reason": m.reason,
            "verdict": m.verdict,
        }
        for r in iterations
        for m in r.all_metric_results  # includes turn-level mount points (Spec §2.2)
        if m.blocking and m.verdict in {"fail", "error"}
    ]
    metric_means: dict[str, float] = {}
    for metric_id in sorted({m.metric for m in all_metrics}):
        mean = metric_mean(all_metrics, metric_id)
        if mean is not None:
            metric_means[metric_id] = mean
    return CaseAggregate(
        case_id=case_id,
        case_version=iterations[0].case_version,
        tags=list(iterations[0].case_tags),
        iterations=len(iterations),
        pass_rate=stability.pass_rate,
        stability=stability.stability,
        regression_state=regression_state,
        valid_iterations=len(valid),
        infra_error_count=stability.infra_error_count,
        tool_calls_mean=round(statistics.mean(len(r.tool_calls) for r in iterations), 6),
        tokens_mean=round(statistics.mean(r.token_count for r in iterations), 6),
        latency_mean=round(statistics.mean(r.latency_ms for r in iterations), 6),
        cost_mean=round(statistics.mean(r.cost for r in iterations), 6),
        score_mean=stability.score_mean,
        score_stddev=stability.score_stddev,
        latency_cv=stability.latency_cv,
        token_variance=stability.token_variance,
        tool_sequence_variance=stability.tool_sequence_variance,
        pass_at_k=dict(stability.pass_at_k),
        metric_means=metric_means,
        blocking_failures=blocking_failures,
        error_semantics=sorted(
            {
                r.failure_semantics.value
                for r in iterations
                if r.status == CaseStatus.ERROR and r.failure_semantics is not None
            }
        ),
        failure_category=next((r.failure_category for r in iterations if r.failure_category), None),
        evaluated_metrics=sum(1 for m in all_metrics if m.verdict != "skipped"),
        artifacts=sorted(
            (
                {
                    "iteration": r.iteration,
                    "name": record.name,
                    "kind": record.kind.value
                    if hasattr(record.kind, "value")
                    else str(record.kind),
                    "path": record.path,
                    "bytes": record.bytes,
                }
                for r in iterations
                for record in r.artifacts
            ),
            key=lambda item: (item["iteration"], item["name"]),
        ),
    )


def compute_verdict(meta: RunMetadata, results: list[CaseRunResult]) -> str:
    """Spec §6.1: partial+infra → infra_failure; blocking FAIL → fail; else pass."""
    if meta.status == RunStatus.partial:
        return "infra_failure"
    if any(r.blocking_failed for r in results):
        return "fail"
    return "pass"


def build_aggregate(
    meta: RunMetadata,
    results: list[CaseRunResult],
    comparison: RegressionComparison | None = None,
) -> RunAggregate:
    states = {c.case_id: c.state for c in (comparison.cases if comparison else [])}
    cases: list[CaseAggregate] = []
    for case_id in sorted({r.case_id for r in results}):
        iterations = sorted([r for r in results if r.case_id == case_id], key=lambda r: r.iteration)
        stability = compute_stability(case_id, iterations)
        cases.append(
            _case_aggregate(
                case_id, iterations, stability, states.get(case_id, RegressionState.UNDETERMINED)
            )
        )

    counts = {
        "cases": len(cases),
        "iterations": len(results),
        "passed_iterations": sum(1 for r in results if r.status == CaseStatus.PASS),
        "failed_iterations": sum(1 for r in results if r.status == CaseStatus.FAIL),
        "error_iterations": sum(1 for r in results if r.status == CaseStatus.ERROR),
        "flaky_cases": sum(1 for c in cases if c.stability == Stability.FLAKY),
        "regression_cases": sum(
            1 for c in cases if c.regression_state == RegressionState.REGRESSION
        ),
        "improved_cases": sum(1 for c in cases if c.regression_state == RegressionState.IMPROVED),
        "undetermined_cases": sum(
            1 for c in cases if c.regression_state == RegressionState.UNDETERMINED
        ),
    }
    warnings: list[str] = []
    if (meta.baseline_mode or BaselineMode.no_baseline.value) == BaselineMode.no_baseline.value:
        reason = meta.baseline_reason or "无合格历史 run"
        warnings.append(
            f"NO BASELINE：dataset_version={meta.dataset_version} {reason}，回归判定不可用"
        )
    if comparison is not None and not comparison.valid:
        warnings.append(f"比较无效：{comparison.invalid_reason}")
    return RunAggregate(
        run=meta,
        cases=cases,
        counts=counts,
        metrics=run_metrics(results),
        verdict=compute_verdict(meta, results),
        comparison=comparison,
        cost=compute_cost_report(results),
        warnings=warnings,
    )


def case_status_for_junit(case: CaseAggregate) -> str:
    """Spec §6.3: failure = blocking FAIL; error = INFRA/EVALUATION; skipped = skipped.

    判定顺序即优先级：ERROR 轮（INFRA/EVALUATION）先落地，然后是 blocking FAIL；
    ``skipped`` 只留给"没有任何一条 metric 真正参与判定"的 case（Spec §19.1.1
    的 skipped 独立结局）。此前该分支挂在 ``valid_iterations == 0`` 上，而那个
    条件必然伴随 ``error_semantics``，于是 skipped 恒为 0，CI 报告里也就看不出
    "这条 case 其实什么都没判"。
    """
    if case.error_semantics:
        return "error"
    if case.blocking_failures:
        return "failure"
    if case.is_unjudged or case.valid_iterations == 0:
        return "skipped"
    return "passed"


def failure_semantics_kind(case: CaseAggregate) -> FailureSemantics | None:
    if not case.error_semantics:
        return None
    # INFRA dominates EVALUATION when both appear (exit-code mapping follows §6.1)
    if FailureSemantics.INFRA.value in case.error_semantics:
        return FailureSemantics.INFRA
    return FailureSemantics(case.error_semantics[0])
