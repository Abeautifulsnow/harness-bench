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
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("deepeval")

from deepeval.models.base_model import DeepEvalBaseLLM  # noqa: E402

from agent_eval.errors import EvaluationInfraError, JudgeInputUnavailableError  # noqa: E402
from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter  # noqa: E402
from agent_eval.evaluators.native import EvalScope  # noqa: E402
from agent_eval.evaluators.registry import DEEPEVAL_AGENT_METRICS, MetricSpec  # noqa: E402
from agent_eval.loading.loader import load_dataset, load_profile  # noqa: E402
from agent_eval.models.case import Case  # noqa: E402
from agent_eval.models.events import TraceEvent  # noqa: E402
from agent_eval.models.results import CaseRunResult, CaseStatus  # noqa: E402
from agent_eval.runner.runner import RunConfig, Runner, _CaseContext, _ResolvedProfile  # noqa: E402
from agent_eval.trace.builder import TraceBuilder  # noqa: E402

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


class _CannedLLM(DeepEvalBaseLLM):
    """有问必答的假 judge 模型：返回一份各 metric schema 的超集 JSON。

    走的是真 SDK：``initialize_model`` 见到 ``DeepEvalBaseLLM`` 原样返回，
    不构造 ``OpenAIModel``，因此**不需要凭据**；而 required params 校验、
    schema 提取、打分、verdict 全是真的。用"调了几次"作为"接线是否真的通了"
    的可观测证据——只断言"拿到分数"会被"其实没调模型"的实现骗过。
    """

    def __init__(self) -> None:
        self.calls = 0

    def load_model(self) -> None:
        return None

    def _payload(self) -> str:
        self.calls += 1
        # 各 metric 的 schema 不同（task_completion 要 verdict/reason、
        # step_efficiency 要 score/reason、plan_quality 还要 plan/task），
        # 返回超集：多出来的键对 pydantic 无影响。
        return json.dumps(
            {
                "verdict": 1.0,
                "reason": "canned",
                "score": 1.0,
                "task": "读取表结构",
                "outcome": "已读取",
                "plan": ["读文件", "汇报"],
            }
        )

    def generate(self, *args: Any, **kwargs: Any) -> str:
        return self._payload()

    async def a_generate(self, *args: Any, **kwargs: Any) -> str:
        return self._payload()

    def get_model_name(self) -> str:
        return "canned-llm"


class TestChatbotJudgeProfileIsWiredEndToEnd:
    """``chatbot-judge`` 档在**真数据集**上的逐条结局：真 profile、真 16 条 case、
    真 adapter，只把 judge 模型换成假替身。

    为什么要有这一层：此前这一档的验证止步于"用本地 stub HTTP 服务跑通一次"
    （run_49ed60607d33）。那证明了**传输层**，没有把"这一档在本数据集上到底判出
    几条、哪几条判不出"固化成断言——而 profile 的注释里恰恰写着这些占比（6/16、
    7/16）。注释会漂移，断言不会。

    实测结论（2026-09-30，tmp/probe_chatbot_judge_pipeline.py 的同一个形状）：
      - ``agent.task_completion``：16/16 条都有输入（本用例给非空 final_output；
        真实运行里"agent 收尾没输出"会走缺输入 → skipped，另有单元测试钉住）
      - ``agent.tool_correctness``：**恰好**在 case 级无 ``tools.required`` 的
        那些 case 上缺输入——与 ``test_required_tools_is_what_gates_tool_correctness``
        是同一事实的上下游两面
      - 两条指标都必须真的走到 judge 模型（stub 调用计数 > 0）
    """

    BENCHMARK = "ai-chatbot-core"
    PROFILE = "chatbot-judge"

    def _dataset(self) -> list[Case]:
        return load_dataset(REPO / "evals", "chatbot-core")[1]

    def _judge_specs(self) -> list[MetricSpec]:
        profile = load_profile(REPO / "evals", self.PROFILE)
        return [spec for spec in profile.metrics if spec.provider == "deepeval"]

    def _tree(self, case: Case):
        """造一份"agent 真的调了它声明要求的工具"的 trace（按协议成对发事件）。"""
        events = [TraceEvent(event_id="evt_run", trace_id="t1", type="run.started", data={})]
        for index, name in enumerate(case.expected.tools.required):
            call_id = f"evt_call_{index}"
            events.append(
                TraceEvent(
                    event_id=call_id,
                    trace_id="t1",
                    type="tool.call",
                    data={"name": name, "arguments": {"path": "x"}},
                )
            )
            events.append(
                TraceEvent(
                    event_id=f"evt_result_{index}",
                    trace_id="t1",
                    parent_span_id=call_id,
                    type="tool.result",
                    data={"status": "ok", "result": "ok"},
                )
            )
        events.append(
            TraceEvent(
                event_id="evt_fin", trace_id="t1", type="run.finished", data={"status": "success"}
            )
        )
        builder = TraceBuilder("t1")
        builder.feed_all(events)
        return builder.build()

    async def _run_case(self, case: Case, stub: _CannedLLM, specs: list[MetricSpec]):
        runner = Runner(
            RunConfig(
                evals_root=REPO / "evals",
                fixtures_root=REPO / "fixtures",
                data_root=REPO / ".agent-eval",
                benchmark=self.BENCHMARK,
                agent_endpoint="fake://",
                baseline_policy="NO_BASELINE",
            )
        )
        profile = load_profile(REPO / "evals", self.PROFILE)
        ctx = _CaseContext(
            resolved={},
            case_profile={},
            case_by_id={},
            judge=runner._resolve_profiles({}, {}, []).judge,
            judge_sem=asyncio.Semaphore(2),
            capabilities={},
            degradations={},
            judge_model=stub,  # type: ignore[arg-type]
        )
        result = CaseRunResult(
            id="cr_probe",
            run_id="run_probe",
            case_id=case.id,
            case_version=case.version,
            iteration=1,
            status=CaseStatus.PASS,
        )
        error = await runner._run_judge_metrics(
            case,
            _ResolvedProfile(profile=profile, judge_specs=specs, harness_specs=[]),
            ctx,
            self._tree(case),
            EvalScope(run_status="success", final_output="占位输出（非空即够判分器用）"),
            result,
        )
        assert error == "", error
        return {m.metric: m for m in result.metric_results}

    def test_profile_declares_exactly_two_judge_metrics(self) -> None:
        assert [spec.id for spec in self._judge_specs()] == [
            "agent.task_completion",
            "agent.tool_correctness",
        ]

    async def test_task_completion_is_judged_on_every_case(self) -> None:
        """16/16 条都能判出分（在输出非空的前提下）——不是"声明了"，是"判到了"。"""
        specs = self._judge_specs()
        stub = _CannedLLM()
        for case in self._dataset():
            rows = await self._run_case(case, stub, specs)
            row = rows["agent.task_completion"]
            assert row.verdict == "pass", f"{case.id}: {row.verdict} / {row.reason}"
            assert row.score == 1.0, case.id
            assert row.metadata.get("deepeval_version"), case.id
        assert stub.calls > 0, "judge 模型一次都没被调用：接线断了，分数是伪造的"

    async def test_tool_correctness_is_skipped_exactly_where_required_tools_are_missing(
        self,
    ) -> None:
        """恒 skipped 的集合必须**恰好**等于"case 级无 required 工具"的集合。

        两侧都从数据集现算，不写死条数：谁补了 ``tools.required``，这条会立刻
        指出来，profile 注释里的占比也就必须跟着改。这挡住两种漂移——
        "缺口比注释里少"（注释过期）与"缺口比预期多"（某条 case 的输入悄悄没了）。
        """
        specs = self._judge_specs()
        stub = _CannedLLM()
        skipped: set[str] = set()
        judged: set[str] = set()
        for case in self._dataset():
            rows = await self._run_case(case, stub, specs)
            row = rows["agent.tool_correctness"]
            if row.verdict == "skipped":
                assert row.metadata.get("skipped_reason") == "judge_input_unavailable", case.id
                assert row.blocking is False, case.id
                skipped.add(case.id)
            else:
                judged.add(case.id)
        no_required = {case.id for case in self._dataset() if not case.expected.tools.required}
        assert skipped == no_required, {
            "多出来的 skip": sorted(skipped - no_required),
            "该 skip 却没 skip": sorted(no_required - skipped),
        }
        assert skipped and judged, (skipped, judged)

    async def test_missing_judge_credentials_stay_infrastructure_not_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """凭据缺失**不得**降级成 skipped（profile 的"未配置即 exit 2"承诺）。

        这是与"输入缺口"方向相反的另一半：输入缺口是**这次观测**的问题（skipped），
        凭据缺失是**run 级配置**问题（infra → EVALUATION_FAILURE → exit 2）。
        把后者记成 skipped 会让"这次评测根本没判"看起来像"这条用例不需要判"。

        用 ``monkeypatch.delenv`` 而不是"本机恰好没配 key"来构造（与 sqlglot 的
        降级路径同一原则：不靠机器状态测）。实测 deepeval 在**构造模型时**读
        ``OPENAI_API_KEY``（不是 import 时缓存），因此 delenv 是有效杠杆。
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        trace = {
            "type": "llm",
            "input": "q",
            "actual_output": "o",
            "tools_called": [{"name": "read_file", "input_parameters": {}}],
            "expected_tools": [{"name": "read_file"}],
        }
        with pytest.raises(EvaluationInfraError):
            await DeepEvalCapabilityAdapter().evaluate("agent.task_completion", 0.7, trace)
