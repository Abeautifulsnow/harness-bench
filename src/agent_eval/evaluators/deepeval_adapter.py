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
        if self._probed is not None:
            return dict(self._probed)
        module = self._load()
        result: dict[str, bool] = {}
        if module is None:
            result = {metric_id: False for metric_id in DEEPEVAL_AGENT_METRICS}
        else:
            for metric_id, class_name in DEEPEVAL_AGENT_METRICS.items():
                try:
                    metrics_mod = importlib.import_module("deepeval.metrics")
                    result[metric_id] = hasattr(metrics_mod, class_name)
                except ImportError:
                    result[metric_id] = False
        self._probed = result
        return dict(result)

    # ---------- Span Tree → DeepEval trace 转换（Adapter 的核心交付物） ----------

    def convert(
        self,
        case: Case,
        tree: SpanTree,
        *,
        final_output: str | None,
        latency_ms: int,
        tokens: int,
    ) -> dict[str, Any]:
        """平台观测 → DeepEval Test Case / Conversation Test Case 字段结构（PRD §37）。"""
        turns_in = case.input.messages()
        return {
            "type": "conversation" if case.input.type == "multi_turn" else "llm",
            "input": turns_in[0] if turns_in else None,
            "turns": [{"role": "user", "content": m} for m in turns_in],
            "actual_output": final_output,
            "expected_output": case.context,
            "context": case.context,
            "retrieval_context": [s.output for s in tree.find("retriever") if s.output is not None],
            "tools_called": tree.tool_sequence(),
            "expected_tools": case.expected.tools.required,
            "completion_time": latency_ms / 1000.0,
            "token_cost": tokens,
        }

    # ---------- metric execution ----------

    async def evaluate(
        self, metric_id: str, threshold: float | None, trace: dict[str, Any]
    ) -> tuple[float | None, str]:
        """执行一个 metric；返回 (score, reason)。SDK 侧一切异常 → EvaluationInfraError。"""
        from agent_eval.errors import EvaluationInfraError

        module = self._load()
        if module is None:
            raise EvaluationInfraError("deepeval is not installed")
        class_name = DEEPEVAL_AGENT_METRICS[metric_id]
        try:
            metrics_mod = importlib.import_module("deepeval.metrics")
            metric_cls = getattr(metrics_mod, class_name)
            test_case = self._build_test_case(trace)
            metric = metric_cls(threshold=threshold)
            score = float(metric.measure(test_case))
        except EvaluationInfraError:
            raise
        except Exception as exc:
            raise EvaluationInfraError(f"deepeval metric '{metric_id}' failed: {exc}") from exc
        reason = (
            "score above threshold"
            if score >= (threshold or 0.0)
            else f"score {score:.3f} below threshold {threshold}"
        )
        return score, reason

    def _build_test_case(self, trace: dict[str, Any]) -> Any:
        """构造 DeepEval Test Case / Conversation Test Case（SDK 类型只在层内出现）。"""
        from agent_eval.errors import EvaluationInfraError

        try:
            test_case_mod = importlib.import_module("deepeval.test_case")
            if trace.get("type") == "conversation":
                conversation = test_case_mod.ConversationTestCase(turns=trace["turns"])
                return conversation
            return test_case_mod.LLMTestCase(
                input=trace.get("input") or "",
                actual_output=trace.get("actual_output") or "",
                expected_output=(trace.get("expected_output") or [None])[0]
                if isinstance(trace.get("expected_output"), list)
                else trace.get("expected_output"),
                context=trace.get("context"),
                retrieval_context=trace.get("retrieval_context") or None,
                tools_called=trace.get("tools_called") or None,
                expected_tools=trace.get("expected_tools") or None,
            )
        except Exception as exc:
            raise EvaluationInfraError(f"deepeval test case construction failed: {exc}") from exc
