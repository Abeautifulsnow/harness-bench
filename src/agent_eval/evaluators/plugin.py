"""Evaluator Plugin SDK（PRD §43 + §109.4，Spec §7.1/§17）。

对外扩展契约：新增 Evaluator **不得修改主 Runner**（PRD §109.4）。平台内部评测早已
插件化（挂载点 / metric id / fallback 链分离），但对外没有扩展点——第三方要加评测
只能改 ``METRIC_REGISTRY`` 与 ``native.py``，正是 PRD §43 想避免的。

设计取舍（Spec §17 记录）：

1. ``EvaluationContext`` 只暴露**已经是稳定契约**的观测面：PRD §8 的 Event Protocol
   （``TraceEvent``）、PRD §10 的 Span Model（``TraceSpan``）、``ToolCallRecord``。
   不把 ``CaseRunResult`` 之类的内部可变结构递出去，否则内部字段一改就是破坏契约。
2. ``MetricResult`` 复用 PRD §45 的 ``MetricResultModel``，不另造类型（PRD §43 的签名）。
   插件不得自填平台所有字段：``metric`` / ``evaluator`` / ``id`` / ``case_run_id``
   由 ``EvaluationContext.result()`` 统一铸造，防止插件把自己的 metric 伪装成
   ``native.status`` 或 ``security.*``（后者是不可被 judge 覆盖的 Hard Gate）。
3. 插件返回值必须与注册名一致；不一致时平台判 ``error``（不静默改名）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent_eval.ids import new_id
from agent_eval.models.events import TraceEvent
from agent_eval.models.results import MetricResultModel, ToolCallRecord
from agent_eval.models.spans import TraceSpan


@dataclass
class EvaluationContext:
    """插件可见的全部观测（一次 CaseRun 的切片）。

    字段来源与稳定性：
      - ``events`` / ``spans``：PRD §8 Event Protocol 与 PRD §10 Span Model，
        是平台最稳定的两个结构；
      - ``tool_calls`` / ``mcp_calls`` / ``command_calls``：Spec §12.1 的三个并列
        观测面（安全规则按此划分，插件同样按此消费）；
      - ``params``：Profile 的 ``MetricSpec.params``，插件的声明式配置入口。
        没有 params，插件就只能靠硬编码阈值，判决口径不可按 case 调整。
    """

    case_run_id: str
    case_id: str
    iteration: int
    tags: list[str] = field(default_factory=list)
    run_status: str = "success"
    final_output: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    mcp_calls: list[ToolCallRecord] = field(default_factory=list)
    command_calls: list[ToolCallRecord] = field(default_factory=list)
    spans: list[TraceSpan] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    latency_ms: int = 0
    tokens: int = 0
    # A2/E2 观测面能力表（事件名 → bool，health 阶段由 SUT 上报）。
    # 插件据此判 skipped：表里显式 False 的事件 = 该观测面不存在。
    # 判定落插件侧而非 resolve_metric——后者对"能力为假"的既有语义是
    # fallback 或 exit 3，承接不了 skipped（change-plan A2 修订四）。
    observation_surface: dict[str, bool] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    threshold: float | None = None
    # 平台铸造返回值的依据：metric id 由平台决定，插件不得自行改写
    metric_id: str = ""

    # ------------------------------------------------------------ 便捷视图

    def events_of(self, event_type: str) -> list[TraceEvent]:
        return [event for event in self.events if event.type == event_type]

    def spans_of(self, span_type: str) -> list[TraceSpan]:
        return [span for span in self.spans if span.type == span_type]

    def tool_sequence(self) -> list[str]:
        """按调用顺序的工具名（PRD §44 的 Loop / Retry 类 evaluator 的基本输入）。"""
        return [span.name for span in self.spans_of("tool")]

    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def missing_observation(self, event_types: tuple[str, ...]) -> list[str]:
        """声明依赖的事件里，哪些被能力表明确声明为"观测面不存在"。

        只有**表里显式 False** 才算缺失；不在表里（含整表为空）按"具备"处理——
        这是既有测试与 FakeAgent 路径行为不变的保证：能力声明是外部接入侧的义务，
        没有声明就没有否决权。绝不允许从"事件没出现"反推"观测不到"（E2 反模式 2）：
        `retry` 事件 0 次与"根本不发 retry 事件"在数据上同形。
        """
        return [event for event in event_types if self.observation_surface.get(event) is False]

    def result(
        self,
        verdict: str,
        *,
        score: float | None = None,
        reason: str = "",
        blocking: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MetricResultModel:
        """铸造 MetricResult：平台字段由平台填，插件只提供判定本身。

        ``blocking`` 缺省 False：**阻断权在 Profile**（``MetricSpec.blocking``），不在插件。
        插件是"这个观测说明了什么"，是否拦门禁是 Gate 策略；让插件按 VERDICT 自行决定
        拦截，会让同一插件在不同 Profile 下无法调整阻断力度（Runner 会以 Profile 覆盖）。
        """
        if blocking is None:
            blocking = False
        return MetricResultModel(
            id=new_id("mr"),
            case_run_id=self.case_run_id,
            metric=self.metric_id,
            evaluator="harness",
            score=score,
            threshold=self.threshold,
            verdict=verdict,  # type: ignore[arg-type]
            blocking=blocking,
            reason=reason,
            metadata={"mount": "harness", **(metadata or {})},
        )


class EvaluatorPlugin(ABC):
    """PRD §43 的公开扩展契约。

    实现者只需给出 ``name``（= 注册的 Metric ID）与 ``evaluate()``。
    ``default_params`` 让插件声明自己认识的配置项（Gate/报告可读）。
    """

    name: str = ""
    description: str = ""
    default_params: dict[str, Any] = {}
    # 本插件依赖的观测面（PRD §8 事件名）。声明表是**框架侧的静态事实**，与 SUT 无关；
    # 能力表（EvaluationContext.observation_surface）把某项声明为 False 时，插件统一判
    # skipped（metadata.skipped_reason="observation_unavailable"，由 run_plugin 执行）。
    # 这是 Spec §19.1.1 三结局（满足/不满足/观测不到）从扩展键升级为所有 metric 通则
    # 的插件侧落点：不声明就等于放弃"观测不到 → skipped"的保护。
    required_events: tuple[str, ...] = ()

    @abstractmethod
    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        """判定一次 CaseRun；返回值必须由 ``context.result()`` 铸造。"""
        raise NotImplementedError


def ratio_score(limit: float, actual: float) -> float:
    """连续分口径（与 Spec §11.1 ``native.step_ratio`` 同形）：``min(1, limit/actual)``。

    统一用比例而非 0/1，是为了让趋势与降级链拿到可比较的分；0/1 会让"轻微超标"
    与"严重超标"在报告里长得一样。
    """
    if actual <= 0:
        return 1.0
    if limit <= 0:
        return 0.0
    return round(min(1.0, limit / actual), 6)


def describe_limit(limit: float, actual: float, unit: str) -> str:
    return f"{unit} {actual:g} > 上限 {limit:g}"
