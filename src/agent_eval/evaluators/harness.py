"""PRD §44 Harness-specific Evaluators（首批四项，确定性判定）。

清单与判定口径（PRD 只给了名字，口径是在实现时定的，写不清的不实现）：

```text
harness.retry             RetryEvaluator            retry 事件数 ≤ max_retries
harness.loop              LoopEvaluator             同工具连续重复 ≤ max_repeats
harness.mcp_permission    MCPPermissionEvaluator    MCP 调用 ⊂ allowed（空则 skipped）
harness.subagent_routing  SubAgentRoutingEvaluator  路由集合 == expected（空则 skipped）
```

未实现的十项及原因见 `docs/agent-eval-engineering-spec-v2.1.md` §17.3——
缺观测数据的（context 压缩 / memory）与口径不清的不做空实现：
恒 PASS 的占位比缺失更危险，它会让覆盖统计说谎。

`blocking` 默认 False（由 Profile 的 MetricSpec 决定）：harness 专项指标是质量信号，
不像安全规则那样是平台底线。
"""

from __future__ import annotations

from agent_eval.evaluators.plugin import (
    EvaluationContext,
    EvaluatorPlugin,
    describe_limit,
    ratio_score,
)
from agent_eval.models.results import MetricResultModel


class RetryEvaluator(EvaluatorPlugin):
    """重试次数（PRD §44 RetryEvaluator）。

    ``retry`` 事件是 PRD §8 协议内的事件类型，重试高发通常意味着 harness 在
    "撞运气"而不是稳定完成任务，因此即使最终成功也值得记一笔。
    """

    name = "harness.retry"
    description = "重试次数不超过声明上限（params.max_retries）"
    default_params = {"max_retries": 0}

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        limit = float(context.param("max_retries", 0) or 0)
        events = context.events_of("retry")
        actual = len(events)
        reasons = [str(event.data.get("reason", "")) for event in events if event.data]
        if actual <= limit:
            return context.result(
                "pass",
                score=ratio_score(limit, actual),
                reason=f"retries {actual} <= 上限 {limit:g}",
                metadata={"retries": actual, "max_retries": limit},
            )
        detail = describe_limit(limit, actual, "retries")
        if any(reasons):
            detail += f"；最近原因：{reasons[-1][:80]}"
        return context.result(
            "fail",
            score=ratio_score(limit, actual),
            reason=detail,
            metadata={"retries": actual, "max_retries": limit},
        )


def longest_consecutive_run(sequence: list[str]) -> tuple[int, str | None]:
    """最长连续相同元素长度（同工具连续重复 = 循环的确定性信号）。

    只认"连续相同"，不认周期为 2 的交替循环（A,B,A,B）：读写交替是正常流程，
    把它判成循环会产生误报——误报比漏报更快让指标失去信誉（Spec §11 的准入原则）。
    """
    best, name, current, previous = 0, None, 0, None
    for item in sequence:
        current = current + 1 if item == previous else 1
        previous = item
        if current > best:
            best, name = current, item
    return best, name


class LoopEvaluator(EvaluatorPlugin):
    """循环检测（PRD §44 LoopEvaluator）：同一工具连续重复调用次数。"""

    name = "harness.loop"
    description = "同一工具的连续重复调用不超过声明上限（params.max_repeats）"
    default_params = {"max_repeats": 3}

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        limit = float(context.param("max_repeats", 3) or 3)
        sequence = context.tool_sequence()
        actual, tool = longest_consecutive_run(sequence)
        metadata = {"longest_repeat": actual, "tool": tool, "max_repeats": limit}
        if actual <= limit:
            return context.result(
                "pass",
                score=ratio_score(limit, actual),
                reason=f"最长连续重复 {actual} <= 上限 {limit:g}",
                metadata=metadata,
            )
        return context.result(
            "fail",
            score=ratio_score(limit, actual),
            reason=f"工具 '{tool}' 连续调用 {actual:g} 次 > 上限 {limit:g}",
            metadata=metadata,
        )


class MCPPermissionEvaluator(EvaluatorPlugin):
    """MCP 越权调用（PRD §44 MCPPermissionEvaluator）。

    与安全侧的 ``security.forbidden_mcp`` 互补：那条是**黑名单**（平台底线，
    不可被 judge 覆盖），这条是**白名单**（MCP 调用的授权集合由 case/profile 声明）。
    白名单为空即不判（skipped）——没有声明就没有依据，凭空全判 pass 是假信号。
    """

    name = "harness.mcp_permission"
    description = "MCP 调用是否都在授权集合内（params.allowed）"
    default_params = {"allowed": []}

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        allowed = [str(name) for name in (context.param("allowed", []) or [])]
        called = [call.name for call in context.mcp_calls]
        if not allowed:
            return context.result(
                "skipped",
                blocking=False,
                reason="未声明 params.allowed：MCP 授权集未定义，本条不判",
                metadata={"mcp_calls": called},
            )
        unauthorized = sorted({name for name in called if name not in allowed})
        metadata = {"allowed": allowed, "mcp_calls": called, "unauthorized": unauthorized}
        if not unauthorized:
            return context.result(
                "pass",
                score=1.0,
                reason=f"MCP 调用均在授权集合内（{len(called)} 次）",
                metadata=metadata,
            )
        return context.result(
            "fail",
            score=0.0,
            reason=f"未授权 MCP 调用：{', '.join(unauthorized)}（授权集 {', '.join(allowed)}）",
            metadata=metadata,
        )


class SubAgentRoutingEvaluator(EvaluatorPlugin):
    """子 Agent 路由（PRD §44 SubAgentRoutingEvaluator）。

    判定"路由到了谁"，不是"路由得好不好"——后者是语义判断，应交给 DeepEval
    （PRD §110-3：能程序判断的不交给 LLM Judge，反过来说需要语义判断的
    也不要伪装成确定性规则）。
    """

    name = "harness.subagent_routing"
    description = "实际路由的子 Agent 集合等于声明集合（params.expected）"
    default_params = {"expected": [], "allow_extra": False}

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        expected = {str(name) for name in (context.param("expected", []) or [])}
        allow_extra = bool(context.param("allow_extra", False))
        observed = {span.name for span in context.spans_of("subagent")}
        if not expected:
            return context.result(
                "skipped",
                blocking=False,
                reason="未声明 params.expected：期望路由未定义，本条不判",
                metadata={"observed": sorted(observed)},
            )
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        problems = []
        if missing:
            problems.append(f"未路由到：{', '.join(missing)}")
        if unexpected and not allow_extra:
            problems.append(f"出现未声明的子 Agent：{', '.join(unexpected)}")
        metadata = {
            "expected": sorted(expected),
            "observed": sorted(observed),
            "missing": missing,
            "unexpected": unexpected,
        }
        if problems:
            return context.result("fail", score=0.0, reason="；".join(problems), metadata=metadata)
        return context.result(
            "pass",
            score=1.0,
            reason=f"路由集合符合声明（{', '.join(sorted(observed)) or '-'}）",
            metadata=metadata,
        )


HARNESS_EVALUATORS: tuple[EvaluatorPlugin, ...] = (
    RetryEvaluator(),
    LoopEvaluator(),
    MCPPermissionEvaluator(),
    SubAgentRoutingEvaluator(),
)
