"""Metric Registry（Spec V2.1.1 §7）：平台级 Metric ID，与任何 SDK 解耦。

命名空间：native.* / agent.* / harness.* / custom.*
fallback 为点号命名空间的 Metric ID（V2.1.1 Errata #3）。

harness.* 是**对外扩展点**（PRD §43/§44）：插件经 ``register_plugin()`` 注册，
``METRIC_REGISTRY`` 只描述它的默认阈值与降级链。新增插件不得修改 Runner
（PRD §109.4）——注册是唯一动作。
"""

from dataclasses import dataclass

from agent_eval.errors import InvalidCallError, MetricUnavailableError
from agent_eval.evaluators.native import unsupported_declarations
from agent_eval.evaluators.plugin import EvaluationContext, EvaluatorPlugin
from agent_eval.models.profile import MetricSpec
from agent_eval.models.results import MetricResultModel

# DeepEval 六个 Agent Metrics（PRD §35 / Spec §7.5，命名以官方为准）
DEEPEVAL_AGENT_METRICS: dict[str, str] = {
    "agent.task_completion": "TaskCompletionMetric",
    "agent.step_efficiency": "StepEfficiencyMetric",
    "agent.tool_correctness": "ToolCorrectnessMetric",
    "agent.argument_correctness": "ArgumentCorrectnessMetric",
    "agent.plan_quality": "PlanQualityMetric",
    "agent.plan_adherence": "PlanAdherenceMetric",
}


@dataclass(frozen=True)
class MetricDef:
    id: str
    provider: str  # native | deepeval | harness
    fallback: str | None = None
    default_threshold: float | None = None
    description: str = ""


METRIC_REGISTRY: dict[str, MetricDef] = {
    # --- native：确定性规则评测，恒可用（Provider=平台自身） ---
    "native.status": MetricDef("native.status", "native", description="run 状态/退出码断言"),
    "native.output_checks": MetricDef(
        "native.output_checks", "native", description="exact/contains/regex/json_schema"
    ),
    "native.tool_sequence": MetricDef(
        "native.tool_sequence", "native", description="required/forbidden tools"
    ),
    "native.argument_checks": MetricDef(
        "native.argument_checks",
        "native",
        description="tool_arguments 声明式参数断言（Spec V2.2 §11.2）",
    ),
    "native.performance": MetricDef(
        "native.performance", "native", description="tool_calls/latency/tokens/cost 上限"
    ),
    "native.step_ratio": MetricDef(
        "native.step_ratio",
        "native",
        description="步数效率 min(1, baseline_steps/actual_steps)（Spec V2.2 §11.1）",
    ),
    # --- agent：DeepEval 语义评测（Spec §7.5）+ 官方 fallback 链 ---
    "agent.task_completion": MetricDef(
        "agent.task_completion", "deepeval", "native.output_checks", 0.70
    ),
    "agent.step_efficiency": MetricDef(
        "agent.step_efficiency", "deepeval", "native.step_ratio", 0.70
    ),
    "agent.tool_correctness": MetricDef(
        "agent.tool_correctness", "deepeval", "native.tool_sequence", 0.80
    ),
    "agent.argument_correctness": MetricDef(
        "agent.argument_correctness", "deepeval", "native.argument_checks"
    ),
    "agent.plan_quality": MetricDef("agent.plan_quality", "deepeval"),
    "agent.plan_adherence": MetricDef("agent.plan_adherence", "deepeval"),
    # --- harness：平台内确定性插件（PRD §43/§44），恒可用，不依赖 judge ---
    "harness.retry": MetricDef(
        "harness.retry", "harness", description="重试次数上限（PRD §44 RetryEvaluator）"
    ),
    "harness.loop": MetricDef(
        "harness.loop", "harness", description="同工具连续重复调用上限（LoopEvaluator）"
    ),
    "harness.mcp_permission": MetricDef(
        "harness.mcp_permission", "harness", description="MCP 授权集合（MCPPermissionEvaluator）"
    ),
    "harness.subagent_routing": MetricDef(
        "harness.subagent_routing",
        "harness",
        description="子 Agent 路由集合（SubAgentRoutingEvaluator）",
    ),
}

# DETERMINISTIC_PROVIDERS：不依赖外部 SDK 的 provider（no_judge 下仍然评测）。
# harness 插件是过程内 Python 判定，与 native 同类；把它当 judge 会让 --no-judge
# 静默跳过一整层评测（PRD §86 只要求"judge 与 agent 并发分离"，不是"关掉确定性评测"）。
DETERMINISTIC_PROVIDERS = frozenset({"native", "harness"})

# 已注册的插件实例：metric id → plugin（PRD §43 的对外扩展点）
PLUGINS: dict[str, EvaluatorPlugin] = {}


def register_plugin(
    plugin: EvaluatorPlugin, *, default_threshold: float | None = None
) -> MetricDef:
    """注册一个 harness 插件（PRD §43/§109.4：这是新增 Evaluator 的唯一动作）。

    插件名必须是 harness.* 命名空间：``custom.*`` 留给用户 GEval，
    ``native.*`` / ``security.*`` 是平台自有语义，不允许外部占用——
    后者尤其重要，``security.*`` 是不可被 judge 覆盖的 Hard Gate。
    """
    name = plugin.name
    if not name.startswith("harness."):
        raise InvalidCallError(f"plugin '{name}' must live in the 'harness.' namespace (Spec §7.1)")
    if name in PLUGINS and PLUGINS[name] is not plugin:
        raise InvalidCallError(f"duplicate evaluator plugin: {name}")
    PLUGINS[name] = plugin
    definition = MetricDef(
        id=name,
        provider="harness",
        default_threshold=default_threshold,
        description=plugin.description,
    )
    METRIC_REGISTRY[name] = definition
    return definition


def plugin_for(metric_id: str) -> EvaluatorPlugin | None:
    return PLUGINS.get(metric_id)


def available_providers_for(spec: MetricSpec) -> bool:
    """该 metric 是否恒可用（无需能力探测）：native 与已注册的 harness 插件。"""
    provider = provider_for(spec)
    if provider == "native":
        return True
    if provider == "harness":
        return spec.id in PLUGINS
    return False


def registry_entry(metric_id: str) -> MetricDef | None:
    return METRIC_REGISTRY.get(metric_id)


def provider_for(spec: MetricSpec) -> str:
    if spec.provider:
        return spec.provider
    entry = registry_entry(spec.id)
    if entry is not None:
        return entry.provider
    # 未在注册表里的 harness.* id 必须按 harness 处理，不能落到 "native" 兜底：
    # ``available_providers_for`` 对 native 一律返回 True，那会让"插件没注册"被
    # 静默当成"有一条 native 规则在评测"——profile 里写错 metric id 就成了空转。
    if spec.id.startswith("harness."):
        return "harness"
    return "native"


def resolve_metric(
    spec: MetricSpec, capabilities: dict[str, bool], no_judge: bool
) -> tuple[str | None, str | None]:
    """按 §7.4 解析 metric → (effective_metric_id | None, degraded_from | None)。

    - 能力可用          → 正常
    - 不可用有 fallback → 降级到 fallback（degraded_from 记录原 id）
    - 不可用无 fallback → MetricUnavailableError（fail-fast，exit 3）
    - no_judge 且 provider 非确定性 → (None, None)：调用方记 skipped

    "确定性"包含已注册的 harness 插件：它们不依赖外部 SDK，不该被 --no-judge 关掉。
    """
    provider = provider_for(spec)
    if no_judge and provider not in DETERMINISTIC_PROVIDERS:
        return None, None
    if capabilities.get(spec.id, available_providers_for(spec)):
        return spec.id, None
    entry = registry_entry(spec.id)
    fallback = spec.fallback or (entry.fallback if entry else None)
    if fallback:
        if not capabilities.get(fallback, fallback.startswith("native.")):
            raise MetricUnavailableError(
                f"metric '{spec.id}' unavailable and fallback '{fallback}' also unavailable"
            )
        return fallback, spec.id
    raise MetricUnavailableError(
        f"metric '{spec.id}' requires provider capability that is unavailable "
        f"and no fallback is declared (Spec §7.4 fail-fast)"
    )


async def run_plugin(plugin: EvaluatorPlugin, context: EvaluationContext) -> MetricResultModel:
    """执行插件并校验返回值（PRD §43）。

    插件返回值必须自报为它注册的 metric：改名会让报告与 Gate 认到不存在的指标，
    静默改名比报错危险得多，因此不一致时判 ``error``（§46 EVALUATION_FAILURE 语义）。
    """
    result = await plugin.evaluate(context)
    if result.metric != plugin.name:
        return context.result(
            "error",
            blocking=False,
            reason=(
                f"plugin '{plugin.name}' returned metric '{result.metric}'；插件不得改名（PRD §43）"
            ),
        )
    return result


def scan_unsupported_assertions(cases: list) -> list[str]:
    """启动期校验：列出 P0 原生评测无法兑现的声明（fail-fast，exit 3）。

    覆盖 session 级两个挂载点（expected / expected.final）与 turn 级 expect；
    重复声明同一问题只报一次，保持报错可读。
    """
    problems: list[str] = []
    for case in cases:
        mounts = case.session_assertions()
        for mount_name, assertion in mounts:
            for detail in unsupported_declarations(assertion, f"expected[{mount_name}]"):
                problems.append(f"{case.id}: {detail}")
        for index, turn in enumerate(getattr(case.input, "turns", []) or []):
            if turn.expect is None:
                continue
            for detail in unsupported_declarations(turn.expect, f"turns[{index}].expect"):
                problems.append(f"{case.id}: {detail}")
    return sorted(set(problems))
