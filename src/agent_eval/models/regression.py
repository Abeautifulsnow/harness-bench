"""Regression / Baseline / Gate models (PRD §53–§59, §64–§69; Spec §3.3/§4/§6).

Regression is a *derived* relation between two CaseRuns (Spec §1.2-3):
``(case_id, dataset_version)`` must match on both sides, otherwise the comparison
is INVALID rather than a REGRESSION.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_eval.models.results import RegressionState, Stability


class BaselineMode(StrEnum):
    """Spec §4.1: three modes + one degradation state (not a mode)."""

    explicit = "explicit"
    release = "release"
    main_latest = "main-latest"
    no_baseline = "NO_BASELINE"


class Baseline(BaseModel):
    """baselines table (Spec §4.4)."""

    id: str
    benchmark_id: str
    dataset_version: str
    mode: BaselineMode
    pinned_run_id: str | None = None  # explicit/release required; cache for main-latest
    pinned_by: str | None = None
    pinned_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    gate_evidence_run_id: str | None = None
    note: str = ""


class MetricDiff(BaseModel):
    """Metric-level diff (PRD §53 metric-level, §105 * Diff 行)."""

    metric: str
    baseline: float | None = None
    candidate: float | None = None
    delta: float | None = None
    delta_percent: float | None = None
    verdict: Literal["improved", "regressed", "unchanged", "undetermined"] = "undetermined"


class PerformanceDiff(BaseModel):
    """PRD §55: candidate may still PASS while tool calls / tokens / latency regress."""

    metric: str  # tool_calls | tokens | latency_ms | cost
    baseline: float | None = None
    candidate: float | None = None
    delta_percent: float | None = None
    regressed: bool = False
    threshold_percent: float | None = None


class CaseDiff(BaseModel):
    case_id: str
    case_version: int = 1
    baseline_stability: Stability = Stability.UNKNOWN
    candidate_stability: Stability = Stability.UNKNOWN
    state: RegressionState = RegressionState.UNDETERMINED
    invalid_reason: str | None = None
    performance: list[PerformanceDiff] = Field(default_factory=list)
    metric_diffs: list[MetricDiff] = Field(default_factory=list)
    baseline_iterations: int = 0
    candidate_iterations: int = 0


class TraceDiffOp(BaseModel):
    """One entry of the aligned tool sequence (PRD §56 annotated diff)."""

    kind: Literal["equal", "added", "removed"]
    value: str
    side: Literal["baseline", "candidate"]


class ArgumentDiff(BaseModel):
    """One tool call argument's diff (PRD §56/§57).

    ``change`` 只记"哪个路径变了"；``comparison`` / ``detail`` / ``degraded`` 记
    "这个结论是按哪一层判出来的"（Spec §20.2）。同一份结论里带层级，是为了让
    报告侧不需要为了"这是语义级相同"改第二处渲染——但**语义级相同不进
    ``argument_diffs``**：那正是本任务要消掉的误报，它只出现在 ``semantic_equal``
    里，避免用户看到空 diff 以为丢数据。
    """

    tool: str
    path: str
    baseline: Any = None
    candidate: Any = None
    change: Literal["added", "removed", "changed"] = "changed"
    comparison: Literal["structural", "semantic", "ast"] = "structural"


class SemanticEqual(BaseModel):
    """判为语义相同的参数路径（Spec §20.2，PRD §57 semantic diff 的可见面）。

    这些路径**文字不同、语义相同**，因此不进 ``argument_diffs``（不产生噪声），
    但要单独列出来：报告的"参数差异为空"必须能与"数据丢了"区分开。
    """

    tool: str
    path: str
    baseline: Any = None
    candidate: Any = None
    comparison: Literal["semantic", "ast"] = "semantic"
    detail: str | None = None
    # 降级分类（sqlglot 缺失 / SQL parse 失败 / 方言未声明）；非空表示这次"相同"
    # 只在低置信层级成立。报告按分类去重（见 TraceDiff.diff_notes）。
    degraded_kind: str | None = None


class TraceDiff(BaseModel):
    """PRD §56 十项对比 + PRD §57 参数结构化 diff。"""

    case_id: str
    baseline_iteration: int = 1
    candidate_iteration: int = 1
    tool_sequence: list[TraceDiffOp] = Field(default_factory=list)
    added_tools: list[str] = Field(default_factory=list)
    removed_tools: list[str] = Field(default_factory=list)
    argument_diffs: list[ArgumentDiff] = Field(default_factory=list)
    # 语义相同的参数路径（PRD §57 semantic diff）：不进 argument_diffs，但必须可见
    semantic_equal: list[SemanticEqual] = Field(default_factory=list)
    # 归一化降级的**去重摘要**（按 kind 去重，Spec §20.2）：让"这次 diff 的结论
    # 有多可信"变成报告里的一个显式事实。逐处的详细报错留在 SemanticEqual.degraded_kind。
    diff_notes: list[str] = Field(default_factory=list)
    model_calls: tuple[int, int] = (0, 0)
    subagent_calls: tuple[int, int] = (0, 0)
    errors: tuple[int, int] = (0, 0)
    retries: tuple[int, int] = (0, 0)
    tokens: tuple[int, int] = (0, 0)
    latency_ms: tuple[int, int] = (0, 0)
    final_answer_changed: bool = False
    changed: bool = False


class FailureCategoryDiff(BaseModel):
    category: str
    baseline: int = 0
    candidate: int = 0
    delta: int = 0


class RegressionComparison(BaseModel):
    """PRD §105 acceptance payload."""

    baseline_run_id: str
    candidate_run_id: str
    benchmark_id: str
    dataset_id: str = ""
    dataset_version: str = ""
    baseline_dataset_version: str = ""
    baseline_mode: str = BaselineMode.no_baseline.value
    valid: bool = True
    invalid_reason: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    metrics: list[MetricDiff] = Field(default_factory=list)
    cases: list[CaseDiff] = Field(default_factory=list)
    flaky_cases: list[str] = Field(default_factory=list)
    performance_regressions: list[str] = Field(default_factory=list)
    trace_diffs: list[TraceDiff] = Field(default_factory=list)
    failure_categories: list[FailureCategoryDiff] = Field(default_factory=list)
    baseline_totals: dict[str, float] = Field(default_factory=dict)
    candidate_totals: dict[str, float] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())


class GateRuleResult(BaseModel):
    """Per-rule machine-readable outcome (Spec §6.2 gate.json)."""

    rule: str
    observed: float | None = None
    threshold: float | None = None
    verdict: Literal["pass", "fail", "undetermined"] = "pass"
    blocking: bool = True
    affected_case_runs: list[str] = Field(default_factory=list)
    detail: str = ""


class GateReport(BaseModel):
    """Spec §6.2 gate.json + §6.3 cross-check block."""

    gate: str
    run_id: str
    verdict: Literal["pass", "fail", "undetermined"] = "pass"
    baseline_mode: str = BaselineMode.no_baseline.value
    baseline_run_id: str | None = None
    rules: list[GateRuleResult] = Field(default_factory=list)
    aggregate: dict[str, int] = Field(default_factory=dict)  # cases/failures/errors/skipped
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
