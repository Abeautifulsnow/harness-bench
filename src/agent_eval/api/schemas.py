"""REST API 响应模型（PRD §81/§82/§84 字段名对齐）。

约定：能被契约模型直接表达的地方（BenchmarkDef / DatasetInfo / Case / SuiteDef /
Experiment / RunMetadata / GateReport / RegressionComparison）一律复用 **平台模型本身**
作为 ``response_model``，这样 OpenAPI schema 与契约不会各自漂移。
本模块只声明"平台模型没有对应物"的派生响应（Dashboard、Trace 视图、Cost、Trend）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_eval.models.run import RunMetadata

ProjectionState = Literal["ok", "missing"]


class ProjectionEnvelope(BaseModel):
    """派生层（DuckDB）查询的统一外壳：投影未构建时不伪装成"没有数据"。"""

    projection: ProjectionState
    hint: str | None = None


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    evals_root: str
    data_root: str
    projection: ProjectionState
    runs: int


class DashboardCard(BaseModel):
    label: str
    value: float | int | str | None = None
    unit: str | None = None
    delta: float | None = None
    detail: str | None = None


class Dashboard(BaseModel):
    """PRD §72 首页字段。"""

    current_release: str | None = None
    release_run_id: str | None = None
    cards: list[DashboardCard] = Field(default_factory=list)
    recent_runs: list[RunMetadata] = Field(default_factory=list)
    projection: ProjectionState = "missing"
    hint: str | None = None


class BenchmarkRow(BaseModel):
    """PRD §73 Benchmark UI 行。"""

    name: str
    version: str | None = None
    description: str = ""
    owner: str | None = None
    dataset: str | None = None
    suites: list[str] = Field(default_factory=list)
    cases: int = 0
    last_run_id: str | None = None
    last_run_at: str | None = None
    pass_rate: float | None = None
    verdict: str | None = None
    regressions: int | None = None
    baseline_mode: str | None = None


class SuiteRow(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    cases: int = 0
    kind: Literal["suite", "security", "red-team"] = "suite"
    description: str = ""


class CaseRow(BaseModel):
    """PRD §73/§14：Case 列表的轻量视图（不返回完整 input/output）。"""

    id: str
    name: str | None = None
    version: int = 1
    dataset_id: str
    tags: list[str] = Field(default_factory=list)
    difficulty: str | None = None
    turns: int = 1
    mount_points: list[str] = Field(default_factory=list)
    description: str = ""


class RunOverview(BaseModel):
    """PRD §75 Run Detail UI Overview tab。"""

    run: RunMetadata
    counts: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    verdict: str = "pass"
    warnings: list[str] = Field(default_factory=list)
    cost: dict[str, Any] = Field(default_factory=dict)
    baseline_mode: str = "NO_BASELINE"


class CaseResultRow(BaseModel):
    case_id: str
    case_run_ids: list[str] = Field(default_factory=list)
    iterations: int = 0
    pass_rate: float = 0.0
    stability: str = "UNKNOWN"
    regression_state: str = "UNDETERMINED"
    valid_iterations: int = 0
    infra_error_count: int = 0
    score_mean: float | None = None
    score_stddev: float | None = None
    tool_calls_mean: float = 0.0
    tokens_mean: float = 0.0
    latency_mean: float = 0.0
    cost_mean: float = 0.0
    pass_at_k: dict[str, float] = Field(default_factory=dict)
    blocking_failures: list[dict[str, Any]] = Field(default_factory=list)
    error_semantics: list[str] = Field(default_factory=list)
    failure_category: str | None = None
    tags: list[str] = Field(default_factory=list)


class ArtifactRow(BaseModel):
    name: str
    path: str
    bytes: int
    content_type: str
    url: str


class ArtifactContent(BaseModel):
    name: str
    content_type: str
    text: str


class CaseArtifactRow(BaseModel):
    """一条 case 级产物（PRD §90，Spec §21.3）。

    ``path`` 是 run 目录相对路径，也是内容端点的唯一解析入口——``name`` 只用于
    展示与定位索引项，绝不拼磁盘路径。
    """

    case_run_id: str
    case_id: str
    iteration: int
    name: str
    kind: str
    path: str
    bytes: int
    collected_at: str
    truncated: bool = False
    note: str | None = None
    content_type: str = "application/octet-stream"
    url: str
    raw_url: str


class CaseArtifacts(BaseModel):
    """某 case 的产物视图：索引 + 采集缺口说明。

    ``unavailable`` 是**能力表**（Spec §21.1），不是错误：采不到的观测面如实列出来，
    这样"没有截图"与"截图采集坏了"在 UI 上长得不一样。``notes`` 是采集期的记账
    （名字非法 / 写盘失败 / provider 抛异常），有内容就说明这次确实少了东西。
    """

    run_id: str
    case_id: str
    items: list[CaseArtifactRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    unavailable: dict[str, str] = Field(default_factory=dict)


class CaseArtifactContent(BaseModel):
    """产物内容预览（仅文本类；二进制回 400 并指向 ``/raw``）。"""

    name: str
    content_type: str
    bytes: int
    text: str = ""
    truncated: bool = False


class SpanNode(BaseModel):
    """PRD §76 Trace Viewer: 节点显示 duration / tokens / input / output / error。"""

    id: str
    parent_span_id: str | None = None
    type: str
    name: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    status: str = "ok"
    error: str | None = None
    tokens: int | None = None
    input: Any = None
    output: Any = None
    metric_results: list[dict[str, Any]] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    children: list[SpanNode] = Field(default_factory=list)


class TraceView(BaseModel):
    run_id: str
    case_id: str
    iteration: int
    trace_id: str | None = None
    span_count: int = 0
    tokens: int = 0
    root: SpanNode | None = None
    tool_sequence: list[str] = Field(default_factory=list)


class TraceEventRow(BaseModel):
    event_id: str
    type: str
    timestamp: str | None = None
    parent_span_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class TraceEvents(ProjectionEnvelope):
    run_id: str
    case_id: str
    iteration: int
    trace_id: str | None = None
    count: int = 0
    events: list[TraceEventRow] = Field(default_factory=list)


class RegressionAnalysis(BaseModel):
    """PRD §77 Regression UI：左右对比 + 各类 diff。"""

    run_id: str
    candidate_run_id: str
    baseline_run_id: str | None = None
    baseline_mode: str = "NO_BASELINE"
    valid: bool = False
    invalid_reason: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    metric_diffs: list[dict[str, Any]] = Field(default_factory=list)
    performance: list[dict[str, Any]] = Field(default_factory=list)
    flaky_cases: list[str] = Field(default_factory=list)
    failure_categories: list[dict[str, Any]] = Field(default_factory=list)
    cases: list[dict[str, Any]] = Field(default_factory=list)
    trace_diffs: list[dict[str, Any]] = Field(default_factory=list)
    baseline_totals: dict[str, float] = Field(default_factory=dict)
    candidate_totals: dict[str, float] = Field(default_factory=dict)


class FailureRow(BaseModel):
    failure_id: str
    case_run_id: str
    case_id: str
    iteration: int
    category: str
    parent: str
    metric: str | None = None
    reason: str | None = None
    evidence: str | None = None
    source: str = "rule"
    tags: list[str] = Field(default_factory=list)


class FailureList(ProjectionEnvelope):
    run_id: str
    total: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    by_parent: dict[str, int] = Field(default_factory=dict)
    by_tool: dict[str, int] = Field(default_factory=dict)
    by_model: dict[str, int] = Field(default_factory=dict)
    by_version: dict[str, int] = Field(default_factory=dict)
    by_benchmark: dict[str, int] = Field(default_factory=dict)
    failures: list[FailureRow] = Field(default_factory=list)


class ClusterRow(BaseModel):
    cluster_id: str
    label: str
    category: str
    parent: str
    size: int
    case_ids: list[str] = Field(default_factory=list)
    representative_case_id: str
    representative_case_run_id: str | None = None
    common_tool_sequence: str = ""
    common_error: str | None = None
    first_seen: str | None = None
    latest_seen: str | None = None
    affected_versions: list[str] = Field(default_factory=list)


class ClusterList(ProjectionEnvelope):
    run_id: str
    total_clusters: int = 0
    total_failures: int = 0
    clusters: list[ClusterRow] = Field(default_factory=list)


class TaxonomyRow(BaseModel):
    category: str
    parent: str
    description: str


class GateRuleRow(BaseModel):
    rule: str
    observed: float | None = None
    threshold: float | None = None
    verdict: str
    blocking: bool = False
    detail: str | None = None


class GateView(BaseModel):
    run_id: str
    gate: str = "pr"
    verdict: str = "undetermined"
    baseline_mode: str = "NO_BASELINE"
    baseline_run_id: str | None = None
    rules: list[GateRuleRow] = Field(default_factory=list)
    aggregate: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    exit_code: int = 0
    source: Literal["stored", "replayed"] = "replayed"


class GateRulesetRow(BaseModel):
    gate: str
    strict: bool = False
    suites: list[str] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    hard_failure_categories: list[str] = Field(default_factory=list)


class ReviewRow(BaseModel):
    id: str
    run_id: str
    case_id: str
    reviewer: str
    verdict: str = ""
    note: str = ""
    queue_reason: str | None = None
    created_at: str | None = None
    case_run_id: str | None = None
    machine_verdict: str | None = None
    status: str = "pending"


class ReviewList(ProjectionEnvelope):
    run_id: str | None = None
    status: str | None = None
    queue_reasons: list[str] = Field(default_factory=list)
    verdicts: list[str] = Field(default_factory=list)
    total: int = 0
    reviews: list[ReviewRow] = Field(default_factory=list)


class ReviewQueueRow(BaseModel):
    case_id: str
    queue_reason: str
    machine_verdict: str | None = None
    iterations: int = 0
    pass_rate: float = 0.0
    stability: str = "UNKNOWN"
    detail: str | None = None


class ReviewQueue(ProjectionEnvelope):
    run_id: str
    total: int = 0
    candidates: list[ReviewQueueRow] = Field(default_factory=list)


class ExperimentRow(BaseModel):
    id: str
    name: str
    benchmark_id: str
    status: str = "created"
    variants: int = 0
    created_at: str | None = None
    dataset_version: str | None = None
    profile: str | None = None
    repeat: int | None = None
    gate: str = "pr"
    note: str = ""


class VariantRow(BaseModel):
    """PRD §74 行：Success / Completion / Efficiency / Tool Accuracy / Cost / Latency。"""

    variant_id: str
    name: str
    dimensions: dict[str, Any] = Field(default_factory=dict)
    status: str = "created"
    run_id: str | None = None
    verdict: str | None = None
    task_success: float | None = None
    task_completion: float | None = None
    step_ratio: float | None = None
    argument_checks: float | None = None
    tool_calls: float | None = None
    tokens: float | None = None
    latency_ms: float | None = None
    cost: float | None = None
    stability: str | None = None
    flaky_cases: int | None = None


class ExperimentDetail(BaseModel):
    experiment: dict[str, Any]
    variants: list[VariantRow] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    groups: dict[str, list[str]] = Field(default_factory=dict)


class CostBreakdown(BaseModel):
    run_id: str
    priced_cases: int = 0
    unpriced_cases: int = 0
    price_configured: bool = False
    total_cost: float | None = None
    agent_cost: float | None = None
    judge_cost: float | None = None
    avg_cost_per_case: float | None = None
    cost_per_success: float | None = None
    judge_cost_ratio: float | None = None
    by_model: dict[str, float] = Field(default_factory=dict)
    note: str | None = None


class TrendPoint(BaseModel):
    run_id: str
    benchmark_id: str | None = None
    agent_model: str | None = None
    git_commit: str | None = None
    dataset_version: str | None = None
    started_at: str | None = None
    verdict: str | None = None
    task_success: float | None = None
    total_cases: int | None = None
    passed_cases: int | None = None
    failed_cases: int | None = None
    total_tokens: int | None = None
    total_cost: float | None = None


class TrendSeries(ProjectionEnvelope):
    benchmark: str | None = None
    dataset_version: str | None = None
    metric: str | None = None
    points: list[TrendPoint] = Field(default_factory=list)
    metric_points: list[dict[str, Any]] = Field(default_factory=list)


class FlakyRow(BaseModel):
    run_id: str
    case_id: str
    iterations: int
    passes: int
    fails: int
    errors: int


class FlakyList(ProjectionEnvelope):
    total: int = 0
    cases: list[FlakyRow] = Field(default_factory=list)


class SecuritySuiteRow(BaseModel):
    name: str
    tag: str
    cases: int
    description: str


class RedTeamCoverageRow(BaseModel):
    category: str
    cases: int
    case_ids: list[str] = Field(default_factory=list)
    covered: bool = False


class SecurityPosture(ProjectionEnvelope):
    run_id: str | None = None
    suites: list[SecuritySuiteRow] = Field(default_factory=list)
    coverage: list[RedTeamCoverageRow] = Field(default_factory=list)
    red_team_categories: list[str] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    failed_findings: int = 0


class BaselineRow(BaseModel):
    id: str
    benchmark_id: str
    dataset_version: str
    mode: str
    pinned_run_id: str | None = None
    pinned_by: str | None = None
    pinned_at: str | None = None
    gate_evidence_run_id: str | None = None
    note: str = ""


class DraftRow(BaseModel):
    id: str
    case_id: str
    case_run_id: str
    suite: str
    draft_status: str
    failure_category: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    created_at: str | None = None


SpanNode.model_rebuild()
