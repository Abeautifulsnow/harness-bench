"""Metric Registry（Spec V2.1.1 §7）：平台级 Metric ID，与任何 SDK 解耦。

命名空间：native.* / agent.* / harness.* / custom.*
fallback 为点号命名空间的 Metric ID（V2.1.1 Errata #3）。
"""

from dataclasses import dataclass

from agent_eval.errors import MetricUnavailableError
from agent_eval.evaluators.native import unsupported_declarations
from agent_eval.models.profile import MetricSpec

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
    "native.performance": MetricDef(
        "native.performance", "native", description="tool_calls/latency/tokens/cost 上限"
    ),
    # --- agent：DeepEval 语义评测（Spec §7.5）+ 官方 fallback 链 ---
    "agent.task_completion": MetricDef(
        "agent.task_completion", "deepeval", "native.output_checks", 0.70
    ),
    "agent.step_efficiency": MetricDef(
        "agent.step_efficiency", "deepeval", "native.performance", 0.70
    ),
    "agent.tool_correctness": MetricDef(
        "agent.tool_correctness", "deepeval", "native.tool_sequence", 0.80
    ),
    "agent.argument_correctness": MetricDef("agent.argument_correctness", "deepeval"),
    "agent.plan_quality": MetricDef("agent.plan_quality", "deepeval"),
    "agent.plan_adherence": MetricDef("agent.plan_adherence", "deepeval"),
}


def registry_entry(metric_id: str) -> MetricDef | None:
    return METRIC_REGISTRY.get(metric_id)


def provider_for(spec: MetricSpec) -> str:
    if spec.provider:
        return spec.provider
    entry = registry_entry(spec.id)
    return entry.provider if entry else "native"


def resolve_metric(
    spec: MetricSpec, capabilities: dict[str, bool], no_judge: bool
) -> tuple[str | None, str | None]:
    """按 §7.4 解析 metric → (effective_metric_id | None, degraded_from | None)。

    - 能力可用          → 正常
    - 不可用有 fallback → 降级到 fallback（degraded_from 记录原 id）
    - 不可用无 fallback → MetricUnavailableError（fail-fast，exit 3）
    - no_judge 且 provider 非 native → (None, None)：调用方记 skipped
    """
    provider = provider_for(spec)
    if no_judge and provider != "native":
        return None, None
    if capabilities.get(spec.id, provider == "native"):
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
