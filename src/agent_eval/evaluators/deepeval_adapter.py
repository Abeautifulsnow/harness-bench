"""DeepEvalCapabilityAdapter（PRD §36 + Spec V2.1.1 §7.3）。

所有 DeepEval SDK 交互集中于本模块：
  - probe()    启动期能力探测（六个 agent.* metric）
  - convert()  平台 Span Tree / Turn 切片 → DeepEval 期望的 trace 结构（核心工作量）
  - evaluate() 单 metric 评分（SDK 缺失或运行失败 → EvaluationInfraError，§46）

deepeval 未安装时 probe 全 False，由 registry 的 fallback 链兜底（§7.4）。
"""

import importlib
import importlib.metadata
from typing import Any

from agent_eval.evaluators.registry import DEEPEVAL_AGENT_METRICS
from agent_eval.models.case import Case
from agent_eval.models.spans import SpanTree


def _expected_output_text(case: Case) -> str | None:
    """PRD §37 的 ``expected_output ← case.expected.output``。

    平台的 ``OutputAssertion`` 没有单一"期望输出文本"字段，它按断言形态表达：
    ``exact`` 是字面期望；只有 ``contains`` 时把命中词表作为期望语义的近似。
    两者都没有（例如只断言工具与约束）时返回 None —— 不拿 ``case.context``
    顶替，那是 judge 的背景说明，当成标准答案会让评分口径整体错位。

    multi-turn 的参照取 ``expected.final.output``（Spec §2.5：expected.final
    作用于最终轮 actual_output，而本 Adapter 的 ``actual_output`` 正是最终轮）。
    只读 case 级 ``expected.output`` 会让多轮判官在没有参照作答的情况下评分
    ——单轮与多轮的判官质量不一致，且恰恰漏掉"多轮逐步细化"这类最需要
    语义判定的用例。
    """
    source = case.expected_final if case.expected_final is not None else case.expected
    expected = source.output
    if expected.exact is not None:
        return expected.exact
    if expected.contains:
        return "\n".join(expected.contains)
    return None


def _tool_calls_from_tree(tree: SpanTree) -> list[dict[str, Any]]:
    """Span Tree 的 tool span → ToolCall 构造参数（PRD §37 ``tools_called ← trace tool calls``）。

    必须保留参数：DeepEval 的 ``ToolCorrectnessMetric`` / ``ArgumentCorrectnessMetric``
    判的就是"调了什么、带什么参数"，只给名字会让参数类指标无从判定。

    参数取 ``attributes["arguments"]``（协议显式声明的 tool.call 参数），不取
    ``span.input``：后者在事件未带 ``arguments`` 时兜底为整份 ``event.data``，
    含 ``name`` 等协议字段，当成"实际发送的参数"等于让 judge 按伪造参数评分。
    """
    calls: list[dict[str, Any]] = []
    for span in tree.find("tool"):
        arguments = span.attributes.get("arguments")
        calls.append(
            {
                "name": span.name,
                "input_parameters": arguments if isinstance(arguments, dict) else None,
            }
        )
    return calls


def _is_missing_input(exc: BaseException) -> bool:
    """该异常是不是"trace 里缺 metric 要的分量"（Spec §19.1.1 第三结局）。

    按类名判而不是 ``isinstance``：``MissingTestCaseParamsError`` 在 deepeval 的
    各版本里模块路径变过（``deepeval.errors`` / ``deepeval.metrics.utils``），
    而这里的调用方已经隔了一层版本无关的适配器，不该再多绑一个内部路径。
    兜底再匹配一次消息文本——SDK 的两条 ``error_str`` 都是固定措辞
    （``cannot be None for the`` / ``cannot be empty for the``），
    拿类名拿不到时仍有判据，比"一律按 infrastruct 处理"安全。
    """
    name = type(exc).__name__
    if name == "MissingTestCaseParamsError":
        return True
    message = str(exc)
    return "cannot be None for the" in message or "cannot be empty for the" in message


class DeepEvalCapabilityAdapter:
    def __init__(self) -> None:
        self._module: Any | None = None
        self._probed: dict[str, bool] | None = None

    # ---------- capability probe (Spec §7.3) ----------

    def _load(self) -> Any | None:
        if self._module is None:
            try:
                self._module = importlib.import_module("deepeval")
            except ImportError:
                self._module = False
        return self._module or None

    def version(self) -> str | None:
        try:
            return importlib.metadata.version("deepeval")
        except importlib.metadata.PackageNotFoundError:
            return None

    def probe(self) -> dict[str, bool]:
        """启动期能力探测（Spec §7.3）。

        探测两件事，缺一不可：
          1. 六个 metric 类在 ``deepeval.metrics`` 里存在；
          2. **Test Case 能用本 Adapter 的实际形状构造出来**（``LLMTestCase`` +
             ``ToolCall``）。

        只做第 1 项是不够的：SDK 大版本里 ``ToolCall`` 字段与 test case 类名都变过，
        "类存在但构造签名对不上"会让启动期探测全绿、运行期每个 case 都判死。
        这里用与 ``_build_test_case()`` 同源的最小样本做一次真实构造，把这类
        不兼容提前到启动期，交给 §7.4 的 fallback 链降级而不是让 run 变 exit 2。

        故意的例外：judge 模型凭据缺失（如未设 ``OPENAI_API_KEY``）不在探测范围。
        那是 run 级配置问题而非 SDK 不兼容，按 §6.1 走 exit 2（评估不可靠）比
        静默降级成 native 更能反映实情。
        """
        if self._probed is not None:
            return dict(self._probed)
        module = self._load()
        if module is None:
            self._probed = {metric_id: False for metric_id in DEEPEVAL_AGENT_METRICS}
            return dict(self._probed)
        try:
            metrics_mod = importlib.import_module("deepeval.metrics")
            test_case_mod = importlib.import_module("deepeval.test_case")
        except ImportError:
            self._probed = {metric_id: False for metric_id in DEEPEVAL_AGENT_METRICS}
            return dict(self._probed)

        shape_ok = self._smoke_build_test_case(test_case_mod)
        self._probed = {
            metric_id: shape_ok and hasattr(metrics_mod, class_name)
            for metric_id, class_name in DEEPEVAL_AGENT_METRICS.items()
        }
        return dict(self._probed)

    @staticmethod
    def _smoke_build_test_case(test_case_mod: Any) -> bool:
        """用最小样本真实构造一次 test case；任何 SDK 形状不兼容都在这里暴露。"""
        try:
            tool_call = test_case_mod.ToolCall(name="probe", input_parameters={"k": "v"})
            test_case_mod.LLMTestCase(
                input="probe",
                actual_output="probe",
                tools_called=[tool_call],
                expected_tools=[test_case_mod.ToolCall(name="probe")],
                completion_time=0.0,
            )
        except Exception:
            return False
        return True

    # ---------- Span Tree → DeepEval trace 转换（Adapter 的核心交付物） ----------

    def convert(
        self,
        case: Case,
        tree: SpanTree,
        *,
        final_output: str | None,
        latency_ms: int,
        cost: float | None = None,
        turn_outputs: list[str | None] | None = None,
    ) -> dict[str, Any]:
        """平台观测 → DeepEval Test Case 字段结构（PRD §37）。

        ``type`` 恒为 ``"llm"``：六个 ``agent.*`` metric 的 ``measure()`` 只接受
        ``LLMTestCase``（已对 SDK 4.2.5 逐个核对），不提供 ``ConversationalTestCase``
        入口。多轮 case 因此按"终局切片 + 前序轮次入 context"表达，而不是交给 SDK
        一个它不消费的对话对象：

          - ``input``            ← 最后一轮的 user 消息（终局评测对象）
          - ``actual_output``    ← run 的最终输出
          - ``context``          ← ``case.context`` + 前序轮次的 user/assistant 文本

        ``turn_outputs`` 是各轮的 assistant 输出（来自 ``CaseRunResult.turn_results``）；
        缺省时前序轮次只有 user 侧，属于观测不足而非编造。
        """
        turns_in = case.input.messages()
        is_multi = case.input.type == "multi_turn"
        context_items = list(case.context or [])

        if is_multi and len(turns_in) > 1:
            # 前序轮次折叠进 context：SDK 不消费 turns，但它们仍是 judge 判"是否准确
            # 解释/是否偏离意图"的必要背景，丢掉等于让多轮 case 退化成单轮首问。
            outputs = list(turn_outputs or [])
            for index, user_message in enumerate(turns_in[:-1]):
                context_items.append(f"user: {user_message}")
                assistant = outputs[index] if index < len(outputs) else None
                if assistant:
                    context_items.append(f"assistant: {assistant}")

        return {
            "type": "llm",
            "case_type": case.input.type,
            "input": turns_in[-1] if is_multi else (turns_in[0] if turns_in else None),
            "actual_output": final_output,
            "expected_output": _expected_output_text(case),
            "context": context_items or None,
            "retrieval_context": [s.output for s in tree.find("retriever") if s.output is not None],
            "tools_called": _tool_calls_from_tree(tree),
            "expected_tools": [{"name": name} for name in case.expected.tools.required],
            # latency_ms=None（未观测，Trace Replay 场景）→ None：SDK 的
            # completion_time 本就可选；不能拿 0.0 顶替（假数据进 judge）。
            "completion_time": (latency_ms / 1000.0) if latency_ms is not None else None,
            # PRD §37 token_cost ← calculated cost。无定价时保持 None（PRD §59：
            # 未知成本绝不写 0.0，否则趋势图会出现"成本降到零"的假象）。
            "token_cost": cost,
            "input_token_count": tree.usage_totals()["input_tokens"],
            "output_token_count": tree.usage_totals()["output_tokens"],
        }

    # ---------- metric execution ----------

    async def evaluate(
        self,
        metric_id: str,
        threshold: float | None,
        trace: dict[str, Any],
        *,
        model: str | None = None,
    ) -> tuple[float | None, str]:
        """执行一个 metric；返回 (score, reason)。SDK 侧一切异常 → EvaluationInfraError。

        ``model`` 是 PRD §91 的 Judge Model：必须落到 ``metric_cls(model=...)``
        才算"Judge 与 Agent 模型分离"。SDK 接受模型名字符串或 ``DeepEvalBaseLLM``。

        **必须走 ``a_measure``，不能走 ``measure``**（C 类第二版实测回修）。
        SDK 4.2.5 的六个 ``agent.*`` metric 默认 ``async_mode=True``，而
        ``measure()`` 的同步分支对其中四个只有 ``pass``、**没有 return**：
        ``self.score`` 只在 ``a_measure`` 里被赋上，``measure()`` 返回 ``None``，
        于是 ``float(None)`` 抛 TypeError → 整条 case 判 EVALUATION_FAILURE。
        实测（第二版探针，见 tests/test_deepeval_judge_metrics.py）：

        ==================== ============== ==========
        metric               measure()      a_measure()
        ==================== ============== ==========
        agent.task_completion   1.0            1.0
        agent.step_efficiency   None           1.0
        agent.tool_correctness  None           1.0
        agent.argument_correctness 1.0         1.0
        agent.plan_quality      None           1.0
        agent.plan_adherence    None           1.0
        ==================== ============== ==========

        四个失灵的指标全部来自 ``nightly`` / ``strict`` 这类收尾档，症状还特别难读：
        报告上是"agent 失败"，实际是判分器自己没返回。另一层理由是本产物本来就是
        异步的——``measure()`` 在事件循环内会走 ``run_until_complete`` +
        ``nest_asyncio`` 的重入 hack，``a_measure`` 才是 SDK 给异步调用方的入口。

        **缺输入 ≠ 判分器故障。** SDK 的 ``MissingTestCaseParamsError`` 表示"这份
        trace 里没有这个 metric 要的分量"（``tools_called`` / ``expected_tools`` 为
        None，或 ``actual_output`` 为空），按 Spec §19.1.1 属于第三结局
        （观测不到 → skipped），因此转成 ``JudgeInputUnavailableError`` 而不是
        ``EvaluationInfraError``：后者会把整条 case 判成 EVALUATION_FAILURE，
        把 run 拉到 exit 2，而 agent 可能什么都没做错。
        """
        from agent_eval.errors import EvaluationInfraError, JudgeInputUnavailableError

        module = self._load()
        if module is None:
            raise EvaluationInfraError("deepeval is not installed")
        class_name = DEEPEVAL_AGENT_METRICS[metric_id]
        try:
            metrics_mod = importlib.import_module("deepeval.metrics")
            metric_cls = getattr(metrics_mod, class_name)
            test_case = self._build_test_case(trace)
            metric = metric_cls(threshold=threshold, **({"model": model} if model else {}))
            raw = await metric.a_measure(test_case)
            if raw is None:
                # 不允许把"判分器没给分"折算成 0：0 分会让报告显示"agent 表现极差"，
                # 而真因是判分器接口不匹配——正是本仓库反复要拦的"假红/假绿"同源问题。
                raise EvaluationInfraError(
                    f"deepeval metric '{metric_id}' returned None: "
                    "该 metric 的 a_measure 未给出分数（判分器接口不匹配，不是 agent 的失败）"
                )
            score = float(raw)
        except (EvaluationInfraError, JudgeInputUnavailableError):
            raise
        except Exception as exc:
            if _is_missing_input(exc):
                raise JudgeInputUnavailableError(str(exc)) from exc
            raise EvaluationInfraError(f"deepeval metric '{metric_id}' failed: {exc}") from exc
        reason = (
            "score above threshold"
            if score >= (threshold or 0.0)
            else f"score {score:.3f} below threshold {threshold}"
        )
        return score, reason

    def _build_test_case(self, trace: dict[str, Any]) -> Any:
        """构造 ``LLMTestCase``（SDK 类型只在层内出现）。

        与 ``probe()._smoke_build_test_case()`` 同源：探测通过 = 这里能构造出来。
        ``tools_called`` / ``expected_tools`` 必须转成 ``ToolCall`` 对象，直接传字符串
        列表会被 SDK 的校验拒绝（``'tools_called' must be None or a list of `ToolCall`）``。
        """
        from agent_eval.errors import EvaluationInfraError

        try:
            test_case_mod = importlib.import_module("deepeval.test_case")
            return test_case_mod.LLMTestCase(
                input=trace.get("input") or "",
                actual_output=trace.get("actual_output") or "",
                expected_output=trace.get("expected_output"),
                context=trace.get("context"),
                retrieval_context=trace.get("retrieval_context") or None,
                tools_called=self._as_tool_calls(test_case_mod, trace.get("tools_called")),
                expected_tools=self._as_tool_calls(test_case_mod, trace.get("expected_tools")),
                completion_time=trace.get("completion_time"),
                token_cost=trace.get("token_cost"),
                input_token_count=trace.get("input_token_count"),
                output_token_count=trace.get("output_token_count"),
            )
        except Exception as exc:
            raise EvaluationInfraError(f"deepeval test case construction failed: {exc}") from exc

    @staticmethod
    def _as_tool_calls(test_case_mod: Any, values: Any) -> list[Any] | None:
        """``{"name": ..., "input_parameters": ...}`` → SDK ``ToolCall`` 列表。"""
        if not values:
            return None
        calls: list[Any] = []
        for value in values:
            if isinstance(value, dict):
                calls.append(
                    test_case_mod.ToolCall(
                        name=value["name"], input_parameters=value.get("input_parameters")
                    )
                )
            else:  # 名字字符串（如 case.expected.tools.required 的退化形态）
                calls.append(test_case_mod.ToolCall(name=str(value)))
        return calls or None
