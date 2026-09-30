"""judge 指标的**输入可判定性**：哪些 metric 在 C 类的 case 形状下有输入。

存在理由（C 类第二版，2026-09-30 实测）：judge 的第二版不是"加两条 profile 条目"
就完事——先得知道本数据集里**哪些 case 根本喂不饱判分器**。这不是推测，是 SDK 的
``check_llm_test_case_params`` 在任何 LLM 调用之前就会给出的结论，因此可以离线
逐条跑出来、并且不需要任何模型凭据。

实测事实（``tmp/probe_judge_params.py`` 的结论在这里固化成断言）：

- ``agent.tool_correctness`` 要 ``tools_called`` **与** ``expected_tools`` 都非空；
  ``expected_tools`` 来自 ``case.expected.tools.required``，因此**不声明 required
  工具的 case 一律喂不饱它**（本数据集 6/16 条）。
- ``agent.task_completion`` / ``agent.step_efficiency`` / ``agent.plan_quality`` /
  ``agent.plan_adherence`` 要非空 ``actual_output``——agent 收尾没输出时同样喂不饱。
- ``agent.argument_correctness`` 只要 ``tools_called``。

这些"喂不饱"必须落 **skipped**（Spec §19.1.1 第三结局），不是 pass 也不是 error：
落 error 会把整条 case 变成 EVALUATION_FAILURE、把 run 拉到 exit 2，而 agent
可能什么都没做错。判定集中在 ``deepeval_adapter._is_missing_input``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("deepeval")

from deepeval.models.base_model import DeepEvalBaseLLM  # noqa: E402

from agent_eval.errors import EvaluationInfraError, JudgeInputUnavailableError  # noqa: E402
from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter  # noqa: E402
from agent_eval.evaluators.registry import DEEPEVAL_AGENT_METRICS  # noqa: E402
from agent_eval.loading.loader import load_dataset  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# 需要 ``actual_output`` 非空的四个（task_completion / step_efficiency /
# plan_quality / plan_adherence）。tool_correctness 与 argument_correctness
# 不看输出，只看工具调用。
NEEDS_OUTPUT = {
    "agent.task_completion",
    "agent.step_efficiency",
    "agent.plan_quality",
    "agent.plan_adherence",
}
NEEDS_EXPECTED_TOOLS = {"agent.tool_correctness"}
NEEDS_CALLED_TOOLS = {
    "agent.tool_correctness",
    "agent.argument_correctness",
}


class _NeverCalledLLM(DeepEvalBaseLLM):
    """替身 judge 模型：被调用即失败。

    这样"输入够不够"就变成"有没有走到 LLM"——一个可观测的事实，而不是我们
    复刻 SDK 的判断。凭据因此不需要（输入不足时压根走不到模型）。
    """

    def __init__(self) -> None:
        pass

    def load_model(self) -> None:
        return None

    def generate(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("PROBE_LLM_CALLED")

    async def a_generate(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("PROBE_LLM_CALLED")

    def get_model_name(self) -> str:
        return "never-called"


def _adapter() -> DeepEvalCapabilityAdapter:
    # 清掉 probe 缓存：本文件不关心能力探测（conftest 默认把它打成不可用）。
    return DeepEvalCapabilityAdapter()


async def _classify(metric_id: str, trace: dict[str, Any]) -> str:
    """返回 'input_unavailable' | 'reached_llm' | 'infra:<msg>'。"""
    try:
        await _adapter().evaluate(metric_id, 0.7, trace, model=_NeverCalledLLM())  # type: ignore[arg-type]
    except JudgeInputUnavailableError:
        return "input_unavailable"
    except EvaluationInfraError as exc:
        if "PROBE_LLM_CALLED" in str(exc):
            return "reached_llm"
        return f"infra:{exc}"
    return "scored"


class TestMissingInputIsSkippedNotFailed:
    """六个 metric 的输入缺口逐条钉住（真实 SDK，无凭据）。"""

    def test_tool_correctness_needs_expected_tools(self) -> None:
        """不声明 required 工具的 case：expected_tools 是 None → 缺输入。"""

        class _Case:
            pass

        trace = {
            "type": "llm",
            "input": "q",
            "actual_output": "o",
            "tools_called": [{"name": "read_file", "input_parameters": {}}],
            "expected_tools": [],
        }
        assert asyncio.run(_classify("agent.tool_correctness", trace)) == "input_unavailable"

    def test_tool_correctness_needs_called_tools(self) -> None:
        """本轮没调工具（负向用例："不许调用任何工具"）→ 同样缺输入。"""
        trace = {
            "type": "llm",
            "input": "q",
            "actual_output": "o",
            "tools_called": [],
            "expected_tools": [{"name": "read_file"}],
        }
        assert asyncio.run(_classify("agent.tool_correctness", trace)) == "input_unavailable"

    @pytest.mark.parametrize("metric_id", sorted(NEEDS_OUTPUT))
    def test_empty_output_is_missing_input_not_infra(self, metric_id: str) -> None:
        """agent 收尾没输出（超时/崩溃收口）→ 缺输入，不是判分器故障。

        这条尤其要紧：超时那类 case 的 ``final_output`` 常常是空串，若按 infra
        处理，一次**已经由 case 自己声明的超时**会额外把 run 抬到 exit 2。
        """
        trace = {
            "type": "llm",
            "input": "q",
            "actual_output": "",
            "tools_called": [{"name": "read_file", "input_parameters": {}}],
            "expected_tools": [{"name": "read_file"}],
        }
        assert asyncio.run(_classify(metric_id, trace)) == "input_unavailable"

    def test_sufficient_input_reaches_the_llm(self) -> None:
        """反方向：输入齐了就必须真的去调 judge 模型（不然上面几条会因为
        "什么输入都判缺"而恒绿）。

        例外只有 ``agent.tool_correctness``：它的判定是**纯确定性**的集合比对
        （``_calculate_score`` 比名字与参数，``available_tools`` 为空时连工具选择
        评分都不请求模型），因此"分数直接算出来、没走 LLM"是它的正常形态。
        真正要钉的是"**不得**被判缺输入"——那才是上面几条的另一面。
        """
        trace = {
            "type": "llm",
            "input": "q",
            "actual_output": "o",
            "tools_called": [{"name": "read_file", "input_parameters": {}}],
            "expected_tools": [{"name": "read_file"}],
        }
        for metric_id in DEEPEVAL_AGENT_METRICS:
            outcome = asyncio.run(_classify(metric_id, trace))
            assert outcome != "input_unavailable", metric_id
            assert not outcome.startswith("infra:"), f"{metric_id}: {outcome}"
            if metric_id == "agent.tool_correctness":
                assert outcome == "scored", metric_id
            else:
                assert outcome == "reached_llm", metric_id


class TestChatbotCoreJudgeInputSplit:
    """把数据集的实际缺口测成断言，而不是写在 profile 注释里。

    缺口变化（有人给某条 case 补上 ``tools.required``、或某条 case 变成 agent
    阶段失败而丢掉输出）时，这条会提醒去复核 profile 的注释与 skip 计数。
    """

    def _dataset(self):
        return load_dataset(REPO / "evals", "chatbot-core")[1]

    def _trace(self, case) -> dict[str, Any]:
        """按 convert() 的口径构造 trace 输入（只关心判分器要的那几个字段）。"""
        return {
            "type": "llm",
            "input": case.input.messages()[-1] if case.input.messages() else "q",
            "actual_output": "占位输出（真实运行里是最终轮 agent 输出）",
            "tools_called": [{"name": "read_file", "input_parameters": {}}],
            "expected_tools": [{"name": name} for name in case.expected.tools.required],
        }

    def test_no_case_produces_an_infra_error_from_input_shape(self) -> None:
        """任何一条 case 都不得因**输入形状**被判成 infra 故障。

        infra 会让整条 case EVALUATION_FAILURE、run 升 exit 2；输入不足只该
        skipped。占位输出保证"输出侧"永远够，因此这条只暴露工具侧的缺口。
        """
        problems: list[str] = []
        for case in self._dataset():
            trace = self._trace(case)
            for metric_id in DEEPEVAL_AGENT_METRICS:
                outcome = asyncio.run(_classify(metric_id, trace))
                if outcome.startswith("infra:"):
                    problems.append(f"{case.id} / {metric_id}: {outcome}")
        assert not problems, problems

    def test_required_tools_is_what_gates_tool_correctness(self) -> None:
        """``agent.tool_correctness`` 的可判定性**恰好**由 required 工具决定。

        这条把"6 条 case 喂不饱它"从注释变成可执行事实：谁给哪条 case 补了
        ``tools.required``，这里会跟着变，profile 的说明也就必须更新。
        """
        gated: set[str] = set()
        ok: set[str] = set()
        for case in self._dataset():
            trace = self._trace(case)
            outcome = asyncio.run(_classify("agent.tool_correctness", trace))
            if not case.expected.tools.required:
                gated.add(case.id)
                assert outcome == "input_unavailable", case.id
            else:
                ok.add(case.id)
                assert outcome != "input_unavailable", case.id
        assert gated, "数据集里应当有未声明 required 工具的 case（负向用例）"
        assert len(gated) + len(ok) == len(self._dataset())
