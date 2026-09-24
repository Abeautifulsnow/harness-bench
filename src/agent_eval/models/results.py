"""Per-iteration results and case aggregates (PRD §45/§82/§83, Spec §3.2)."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_eval.models.run import FailureSemantics


class CaseStatus(StrEnum):
    """Iteration verdict. ERROR iterations are excluded from stability stats (Spec §3.1)."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


class Stability(StrEnum):
    STABLE_PASS = "STABLE_PASS"
    STABLE_FAIL = "STABLE_FAIL"
    FLAKY = "FLAKY"
    UNKNOWN = "UNKNOWN"


class RegressionState(StrEnum):
    """PRD §54 + Spec §3.3. P0 has no baseline storage → UNDETERMINED / INVALID only."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    REGRESSION = "REGRESSION"
    IMPROVED = "IMPROVED"
    UNCHANGED = "UNCHANGED"
    FLAKY = "FLAKY"
    UNDETERMINED = "UNDETERMINED"
    INVALID = "INVALID"


class MetricResultModel(BaseModel):
    """One evaluator verdict on one CaseRun (PRD §45/§83)."""

    id: str
    case_run_id: str
    metric: str  # platform metric id (Spec §7.1 namespace)
    evaluator: str  # native | deepeval | <plugin name>
    score: float | None = None
    threshold: float | None = None
    verdict: Literal["pass", "fail", "warning", "error", "skipped"]
    blocking: bool = False
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None  # from tool.result event


class TurnResult(BaseModel):
    index: int  # 1-based turn number
    output: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    # Spec §12.1：MCP 调用与 command.* 事件是独立观测面，不与 tool_calls 混同
    mcp_calls: list[ToolCallRecord] = Field(default_factory=list)
    command_calls: list[ToolCallRecord] = Field(default_factory=list)
    latency_ms: int = 0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float | None = None  # None = 该 turn 无定价可算（PRD §59）
    status: Literal["ok", "timeout", "error"] = "ok"
    error: str | None = None
    metric_results: list[MetricResultModel] = Field(default_factory=list)


class CaseRunResult(BaseModel):
    """One iteration of one case (PRD §82 case_runs)."""

    id: str
    run_id: str
    case_id: str
    case_version: int
    case_tags: list[str] = Field(default_factory=list)  # denormalized for suite-level slicing
    iteration: int  # 1-based
    status: CaseStatus = CaseStatus.FAIL
    failure_semantics: FailureSemantics | None = None
    # failures-table conclusions are cached here as denormalized read model
    # (PRD §82 as amended by V2.0.1); source of truth is the failures layer.
    failure_category: str | None = None
    latency_ms: int = 0
    token_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float = 0.0  # 0.0 = 无定价可算（PRD §59）；判定见 metric_results 的 cost 说明
    cost_known: bool = False  # True 表示 cost 由定价表算出，0.0 才是真值
    final_output: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    # Spec §12.1：MCP 调用与 command.* 事件的可执行名（不与 tool_calls 混同），
    # 供 security.forbidden_mcp / forbidden_command 消费
    mcp_calls: list[ToolCallRecord] = Field(default_factory=list)
    command_calls: list[ToolCallRecord] = Field(default_factory=list)
    turn_results: list[TurnResult] = Field(default_factory=list)
    metric_results: list[MetricResultModel] = Field(default_factory=list)
    error: str | None = None
    trace_path: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def all_metric_results(self) -> list[MetricResultModel]:
        """本次执行声明的全部判定：case 级 + 各 turn 级（Spec §2.2 三个挂载点）。

        case 判决、report、gate 都必须读这个聚合视图；只看 case 级会让 turn 级
        断言"算了但不算数"，导致违反自身声明的 case 仍报 PASS。
        """
        results = list(self.metric_results)
        for turn in self.turn_results:
            results.extend(turn.metric_results)
        return results

    @property
    def blocking_failed(self) -> bool:
        return any(m.blocking and m.verdict in {"fail", "error"} for m in self.all_metric_results)


class CaseStability(BaseModel):
    """Case-level aggregate over iterations (Spec §3.2 field names are canonical)."""

    case_id: str
    stability: Stability = Stability.UNKNOWN
    pass_rate: float = 0.0
    valid_iterations: int = 0
    infra_error_count: int = 0
    score_mean: float | None = None
    score_stddev: float | None = None
    tool_sequence_variance: float | None = None
    latency_cv: float | None = None
    token_variance: float | None = None
    # PRD §31: pass@k 仅在 repeat >= k 时输出（repeat=1 的 PR Gate 不输出）
    pass_at_k: dict[str, float] = Field(default_factory=dict)
    regression_state: RegressionState = RegressionState.UNDETERMINED
