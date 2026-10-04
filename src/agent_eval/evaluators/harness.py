"""PRD §44 Harness-specific Evaluators（首批四项，确定性判定）。

清单与判定口径（PRD 只给了名字，口径是在实现时定的，写不清的不实现）：

```text
harness.retry               RetryEvaluator              retry 事件数 ≤ max_retries
harness.loop                LoopEvaluator               同工具连续重复 ≤ max_repeats
harness.mcp_permission      MCPPermissionEvaluator      MCP 调用 ⊂ allowed（空则 skipped）
harness.subagent_routing    SubAgentRoutingEvaluator    路由集合 == expected（空则 skipped）
harness.skill_load          SkillLoadEvaluator          加载的 skill 集合/首个 == 声明
harness.context_compaction  ContextCompactionEvaluator  压缩次数在声明区间内
```

未实现的八项及原因见 `docs/agent-eval-engineering-spec-v2.1.md` §17.3——
缺观测数据的（memory / recovery）与口径不清的不做空实现：
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
    # 观测面依赖（A2）：retry 事件是本条的唯一判定输入。SUT 只写日志不发事件时
    # （重试发生在 provider 内部、不上协议流），0 次观测与"没有重试"同形——
    # 不声明这条依赖，`0 <= 0` 的恒 pass 就是假信号。
    required_events = ("retry",)

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
    # tool_sequence 由 tool.call 事件派生（A2）：事件面不存在时序列恒空、
    # 最长重复恒 0——与 retry 同病的另一处，必须一起声明。
    required_events = ("tool.call",)

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
    未声明即不判（skipped）——没有声明就没有依据，凭空全判 pass 是假信号。
    """

    name = "harness.mcp_permission"
    description = "MCP 调用是否都在授权集合内（params.allowed）"
    # 缺省 None 而不是 []：`allowed: []` 是一个真断言（"任何 MCP 调用都算越权"），
    # 与"没声明"必须可区分——两者都写成 [] 会让前者退化成 skipped（静默不判）。
    default_params = {"allowed": None}
    # mcp span 由 mcp.call 事件派生（trace/builder.py 配对表）：SUT 把 MCP 调用
    # 折叠进普通工具流时 span 恒空、越权集合恒空 → 判 pass。声明依赖后该形态
    # 判 skipped（B2：方言必须拆流，这条是漏拆时的安全网）。
    required_events = ("mcp.call",)

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        declared = context.param("allowed")
        allowed = [str(name) for name in (declared or [])]
        called = [call.name for call in context.mcp_calls]
        if declared is None:
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
    # 同 mcp_permission：`expected: []`（不得路由任何子 Agent）是真断言，缺省须为 None
    default_params = {"expected": None, "allow_extra": False}
    required_events = ("subagent.started", "subagent.finished")

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        declared = context.param("expected")
        expected = {str(name) for name in (declared or [])}
        allow_extra = bool(context.param("allow_extra", False))
        observed = {span.name for span in context.spans_of("subagent")}
        if declared is None:
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


class SkillLoadEvaluator(EvaluatorPlugin):
    """Skill 加载与优先级（PRD §44 SkillPriorityEvaluator + SkillLoadEvaluator）。

    两者合为一个 metric：观测面是同一组 ``skill.*`` 事件，拆成两条只会得到
    "永远同时红/同时绿"的两个指标。优先级用 ``params.expected_first`` 表达——
    "多个 skill 同时匹配时哪个该赢"在事件流里就是 ``skill.loaded`` 的顺序。

    只判"加载了谁/谁在前"，不判"该不该加载"：后者需要语义判断（PRD §110-3）。
    四个参数都未声明时判 skipped。

    ``expected_loaded`` 声明后按**集合双向**比对（C 类勘察修正）：少了判"未加载"，
    多了判"未声明的 skill"（``allow_extra: true`` 可放宽）。修正前的实现只查
    ``missing``，于是 ``expected_loaded: []`` —— 本文件与 `skill.fallback.no_skill`
    都写明它是"本条不得加载任何 skill"的真断言 —— 实际**恒 pass**：一条怎么都不会
    红的声明正是 §19.1 要拦的假绿。集合比对是既有先例（``SubAgentRoutingEvaluator``
    的 ``allow_extra``），两个插件现在同构。
    """

    name = "harness.skill_load"
    description = "实际加载/首个加载的 skill 符合声明（params.expected_loaded / expected_first）"
    # 三个参数缺省 None：`expected_loaded: []`（不得加载任何 skill）与"没声明"必须
    # 可区分，否则前者会退化成 skipped——那正是"该红的时候不红"。
    default_params = {
        "expected_loaded": None,
        "expected_first": None,
        "max_loaded": None,
        "allow_extra": False,
    }
    required_events = ("skill.loaded",)

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        declared_loaded = context.param("expected_loaded")
        expected = [str(name) for name in (declared_loaded or [])]
        first = context.param("expected_first")
        max_loaded = context.param("max_loaded")
        allow_extra = bool(context.param("allow_extra", False))
        loaded = [str(event.data.get("name", "")) for event in context.events_of("skill.loaded")]
        loaded = [name for name in loaded if name]
        discovered = sorted(
            {str(event.data.get("name", "")) for event in context.events_of("skill.discovered")}
            - {""}
        )
        if declared_loaded is None and first is None and max_loaded is None:
            return context.result(
                "skipped",
                blocking=False,
                reason="未声明 params.expected_loaded/expected_first/max_loaded：本条不判",
                metadata={"loaded": loaded, "discovered": discovered},
            )
        problems: list[str] = []
        missing = [name for name in expected if name not in loaded]
        if missing:
            problems.append(f"未加载：{', '.join(missing)}")
        # 未声明的加载只在**声明了集合**时才判：没声明集合就没有"多出来的"这回事
        # （与 subagent_routing 一致——那时只判 expected_first / max_loaded）。
        unexpected = (
            [name for name in loaded if name not in expected] if declared_loaded is not None else []
        )
        if unexpected and not allow_extra:
            problems.append(f"出现未声明的 skill：{', '.join(unexpected)}")
        if first is not None and (not loaded or loaded[0] != str(first)):
            actual_first = loaded[0] if loaded else "-"
            problems.append(f"首个加载的 skill 应为 '{first}'，实际 '{actual_first}'")
        if max_loaded is not None and len(loaded) > int(max_loaded):
            problems.append(f"加载 {len(loaded)} 个 skill > 上限 {int(max_loaded)}")
        metadata = {
            "loaded": loaded,
            "discovered": discovered,
            "expected_loaded": expected,
            "expected_first": first,
            "unexpected": unexpected,
        }
        if problems:
            return context.result("fail", score=0.0, reason="；".join(problems), metadata=metadata)
        return context.result(
            "pass",
            score=1.0,
            reason=f"skill 加载符合声明（{', '.join(loaded) or '-'}）",
            metadata=metadata,
        )


class ContextCompactionEvaluator(EvaluatorPlugin):
    """上下文压缩次数（PRD §44 ContextCompressionEvaluator）。

    观测面是 ``context.compaction.started`` 事件。判"压缩了几次"，不判"压缩得好不好"：
    压缩后**保留了哪些事实**在事件流里不可观测（Spec §17.3），只能靠最终输出间接验证，
    那是 ``output_checks`` 的职责，不应伪装成本指标的判定。

    真正要拦的失败模式是压缩循环：压完仍超限 → 再压 → 反复。次数上限因此是
    `always judgeable` 的一半，另一半是"该压的时候没压"（``expected_compactions``）。
    """

    name = "harness.context_compaction"
    description = "上下文压缩次数在声明区间内（params.expected_compactions/max_compactions）"
    default_params = {"expected_compactions": None, "max_compactions": 3}
    # started/finished 必须成对观测（只认配对关闭的压缩）：任一侧缺失即整条不可判。
    required_events = ("context.compaction.started", "context.compaction.finished")

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        expected = context.param("expected_compactions")
        max_compactions = context.param("max_compactions")
        started = [event for event in context.events_of("context.compaction.started")]
        # 只统计配对关闭的压缩：开了没关是 harness 崩溃，不能算"压缩过一次"
        finished_ids = {
            event.parent_span_id
            for event in context.events_of("context.compaction.finished")
            if event.parent_span_id
        }
        completed = [event for event in started if event.event_id in finished_ids]
        metadata = {
            "compactions": len(completed),
            "started": len(started),
            "expected_compactions": expected,
            "max_compactions": max_compactions,
        }
        if expected is None and max_compactions is None:
            return context.result(
                "skipped",
                blocking=False,
                reason="未声明压缩次数约束：本条不判",
                metadata=metadata,
            )
        problems: list[str] = []
        if expected is not None and len(completed) != int(expected):
            problems.append(f"压缩 {len(completed)} 次，声明为 {int(expected)} 次")
        if max_compactions is not None and len(completed) > int(max_compactions):
            problems.append(f"压缩 {len(completed)} 次 > 上限 {int(max_compactions)}（压缩循环）")
        if len(started) != len(completed):
            problems.append(f"{len(started) - len(completed)} 次压缩未正常结束")
        if problems:
            return context.result("fail", score=0.0, reason="；".join(problems), metadata=metadata)
        return context.result(
            "pass",
            score=1.0,
            reason=f"上下文压缩 {len(completed)} 次，符合声明",
            metadata=metadata,
        )


HARNESS_EVALUATORS: tuple[EvaluatorPlugin, ...] = (
    RetryEvaluator(),
    LoopEvaluator(),
    MCPPermissionEvaluator(),
    SubAgentRoutingEvaluator(),
    SkillLoadEvaluator(),
    ContextCompactionEvaluator(),
)
