"""Judge Cost Optimization 的跳过策略（PRD §92）。

§92 的执行顺序是 Native → 明显 Hard Failure → 按策略跳过部分高成本 Judge。
``skip_blocked`` 实现的是第三层：case 已被阻断性失败判死时，非阻断 judge
不再花钱。两个保守条件的方向都是"不能因省钱改判定"——测试按条件逐条钉住。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter
from agent_eval.evaluators.native import EvalScope
from agent_eval.evaluators.registry import MetricSpec
from agent_eval.models.case import Case
from agent_eval.models.profile import MetricProfile
from agent_eval.models.results import CaseRunResult, CaseStatus, MetricResultModel
from agent_eval.runner.runner import RunConfig, Runner, _judge_skip_reason
from agent_eval.trace.builder import TraceBuilder

REPO = Path(__file__).resolve().parents[1]


def _result(blocked: bool) -> CaseRunResult:
    metric_results = [
        MetricResultModel(
            id="mr_native",
            case_run_id="cr1",
            metric="native.output_checks",
            evaluator="native",
            verdict="fail" if blocked else "pass",
            blocking=True,
            reason="expected to contain 'x', got ''" if blocked else "ok",
        )
    ]
    return CaseRunResult(
        id="cr1",
        run_id="run_x",
        case_id="c1",
        case_version=1,
        iteration=1,
        status=CaseStatus.FAIL if blocked else CaseStatus.PASS,
        metric_results=metric_results,
    )


class TestJudgeSkipReason:
    SPEC = MetricSpec(id="agent.task_completion", threshold=0.7)
    BLOCKING_SPEC = MetricSpec(id="agent.task_completion", threshold=0.7, blocking=True)

    def test_policy_off_never_skips(self) -> None:
        assert _judge_skip_reason(_result(True), [self.SPEC], "none") == ""

    def test_blocked_case_skips_non_blocking_judge(self) -> None:
        reason = _judge_skip_reason(_result(True), [self.SPEC], "skip_blocked")
        assert "PRD §92" in reason

    def test_case_not_blocked_does_not_skip(self) -> None:
        """判定未定时 judge 仍要花钱跑：它的分数还没有"不会改结论"的保证。"""
        assert _judge_skip_reason(_result(False), [self.SPEC], "skip_blocked") == ""

    def test_blocking_judge_spec_never_skips(self) -> None:
        """blocking 的 judge 参与判定，跳过等于改判——宁可贵也要跑。"""
        assert _judge_skip_reason(_result(True), [self.BLOCKING_SPEC], "skip_blocked") == ""

    def test_error_verdict_also_counts_as_blocked(self) -> None:
        result = _result(True)
        result.metric_results[0].verdict = "error"
        assert _judge_skip_reason(result, [self.SPEC], "skip_blocked") != ""


class TestJudgeSkipWiring:
    async def test_judge_metrics_are_recorded_as_skipped(self, evals_tree, fixtures_root) -> None:
        """接线：跳过必须留痕为 skipped 的 metric result，而不是静默不产出。

        ``skipped`` 是独立结局（Spec §19.1.1）；静默不产出会让"这条 judge 没跑"
        看起来像"本来就没有 judge"。走真实 Runner 但在 convert 之前跳过，
        因此不需要 judge 凭据 / 真 SDK 调用。
        """
        from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter
        from agent_eval.runner.runner import _CaseContext, _ResolvedProfile

        evals_root, data_root = evals_tree
        runner = Runner(
            RunConfig(
                evals_root=evals_root,
                fixtures_root=fixtures_root,
                data_root=data_root,
                benchmark="database-core",
                agent_endpoint="fake://",
                suites=["smoke"],
                judge_skip_policy="skip_blocked",
                baseline_policy="NO_BASELINE",
            )
        )
        profile = MetricProfile.model_validate(
            {"name": "p", "metrics": [{"id": "agent.task_completion", "provider": "deepeval"}]}
        )
        resolved = _ResolvedProfile(
            profile=profile,
            judge_specs=[MetricSpec(id="agent.task_completion", threshold=0.7)],
            harness_specs=[],
        )
        ctx = _CaseContext(
            resolved={"p": resolved},
            case_profile={"c1": "p"},
            case_by_id={},
            judge=DeepEvalCapabilityAdapter(),
            judge_sem=asyncio.Semaphore(1),
            capabilities={},
            degradations={},
        )
        case = Case.model_validate(
            {
                "id": "c1",
                "version": 1,
                "name": "c1",
                "input": {"type": "single_turn", "prompt": "q"},
            }
        )
        result = _result(blocked=True)

        error = await runner._run_judge_metrics(
            case, resolved, ctx, TraceBuilder().build(), _scope(), result
        )

        assert error == ""
        judge_rows = [m for m in result.metric_results if m.evaluator == "deepeval"]
        assert len(judge_rows) == 1
        assert judge_rows[0].verdict == "skipped"
        assert judge_rows[0].metadata.get("policy") == "skip_blocked"
        assert "PRD §92" in judge_rows[0].reason


class TestJudgeInputUnavailable:
    """judge 的输入在本次观测里不存在 → skipped，**不是** EVALUATION_FAILURE。

    C 类第二版实测（2026-09-30）：`chatbot-core` 16 条 case 里 6 条不声明
    `tools.required`、7 条没有 `expected.output`。SDK 的
    `check_llm_test_case_params` 对这两种缺失直接抛 `MissingTestCaseParamsError`。
    若把它当基础设施故障处理，整条 case 变成 EVALUATION_FAILURE、run 升到
    exit 2 —— 而 agent 可能什么都没做错，只是这条用例本来就没给判分器参照。
    两个方向都要钉住：缺输入落 skipped，判分器真坏仍落 error。
    """

    async def _run(self, judge: object) -> tuple[str, CaseRunResult]:
        from agent_eval.runner.runner import _CaseContext, _ResolvedProfile

        profile = MetricProfile.model_validate(
            {"name": "p", "metrics": [{"id": "agent.task_completion", "provider": "deepeval"}]}
        )
        resolved = _ResolvedProfile(
            profile=profile,
            judge_specs=[MetricSpec(id="agent.task_completion", threshold=0.7)],
            harness_specs=[],
        )
        ctx = _CaseContext(
            resolved={"p": resolved},
            case_profile={"c1": "p"},
            case_by_id={},
            judge=judge,
            judge_sem=asyncio.Semaphore(1),
            capabilities={},
            degradations={},
        )
        case = Case.model_validate(
            {
                "id": "c1",
                "version": 1,
                "name": "c1",
                "input": {"type": "single_turn", "prompt": "q"},
            }
        )
        result = _result(blocked=False)
        runner = Runner(
            RunConfig(
                evals_root=REPO / "evals",
                fixtures_root=REPO / "fixtures",
                data_root=REPO / ".agent-eval",
                benchmark="database-core",
                agent_endpoint="fake://",
                suites=["smoke"],
                baseline_policy="NO_BASELINE",
            )
        )
        error = await runner._run_judge_metrics(
            case, resolved, ctx, TraceBuilder().build(), _scope(), result
        )
        return error, result

    async def test_missing_input_becomes_skipped_not_error(self) -> None:
        from agent_eval.errors import JudgeInputUnavailableError

        class MissingInputJudge(DeepEvalCapabilityAdapter):
            """只替换 evaluate：convert/version 走真实现，形状与生产一致。"""

            async def evaluate(self, *args: object, **kwargs: object) -> tuple[float, str]:
                raise JudgeInputUnavailableError(
                    "'tools_called' cannot be None for the 'Tool Correctness' metric"
                )

        error, result = await self._run(MissingInputJudge())

        assert error == "", "缺输入不得把 case 拉成 EVALUATION_FAILURE（那会让 run 变 exit 2）"
        rows = [m for m in result.metric_results if m.evaluator == "deepeval"]
        assert len(rows) == 1
        assert rows[0].verdict == "skipped", "没判过就是没判过，不得落 pass 也不得落 error"
        assert rows[0].blocking is False
        assert rows[0].metadata.get("skipped_reason") == "judge_input_unavailable"
        assert "§19.1.1" in rows[0].reason

    async def test_real_judge_failure_still_reads_as_infrastructure(self) -> None:
        """反方向：判分器真坏了仍是 error → EVALUATION_FAILURE（PRD §46）。"""

        class BrokenJudge(DeepEvalCapabilityAdapter):
            async def evaluate(self, *args: object, **kwargs: object) -> tuple[float, str]:
                raise RuntimeError("connection reset")

        error, result = await self._run(BrokenJudge())

        assert "failed" in error
        assert not [m for m in result.metric_results if m.evaluator == "deepeval"]


def _scope() -> EvalScope:
    return EvalScope(run_status="success", final_output="out")
