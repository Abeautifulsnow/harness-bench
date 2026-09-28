"""Metric Registry 解析（§7.4）与 DeepEvalCapabilityAdapter 测试。"""

from __future__ import annotations

import sys
import types

import pytest

from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter
from agent_eval.evaluators.registry import (
    DEEPEVAL_AGENT_METRICS,
    METRIC_REGISTRY,
    MetricUnavailableError,
    resolve_metric,
    scan_unsupported_assertions,
)
from agent_eval.models.case import Case
from agent_eval.models.profile import MetricSpec


def test_native_always_available() -> None:
    spec = MetricSpec(id="native.output_checks")
    effective, degraded = resolve_metric(spec, {}, no_judge=True)
    assert effective == "native.output_checks" and degraded is None


def test_fallback_applied_and_recorded() -> None:
    spec = MetricSpec(id="agent.task_completion", fallback="native.output_checks")
    caps = {"agent.task_completion": False, "native.output_checks": True}
    effective, degraded = resolve_metric(spec, caps, no_judge=False)
    assert effective == "native.output_checks"
    assert degraded == "agent.task_completion"


def test_registry_fallback_used_when_spec_has_none() -> None:
    spec = MetricSpec(id="agent.tool_correctness")  # registry 自带 fallback
    caps = {k: False for k in METRIC_REGISTRY}
    caps["native.tool_sequence"] = True
    effective, degraded = resolve_metric(spec, caps, no_judge=False)
    assert effective == "native.tool_sequence" and degraded == "agent.tool_correctness"


def test_fail_fast_without_fallback() -> None:
    spec = MetricSpec(id="agent.plan_quality")  # registry 无 fallback
    caps = {"agent.plan_quality": False}
    with pytest.raises(MetricUnavailableError):
        resolve_metric(spec, caps, no_judge=False)


def test_no_judge_skips_judge_metrics() -> None:
    spec = MetricSpec(id="agent.task_completion")
    effective, degraded = resolve_metric(spec, {"agent.task_completion": True}, no_judge=True)
    assert effective is None and degraded is None


@pytest.mark.real_judge
def test_deepeval_probe_without_package() -> None:
    adapter = DeepEvalCapabilityAdapter()
    probe = adapter.probe()
    assert len(probe) == 6
    # 环境未安装 deepeval（CI/开发机默认）；若安装了则全 True
    installed = adapter.version() is not None
    assert all(probe.values()) if installed else not any(probe.values())


def test_deepeval_convert_shape() -> None:
    from agent_eval.models.events import TraceEvent
    from agent_eval.trace.builder import TraceBuilder

    case = Case.model_validate(
        {
            "id": "c",
            "version": 1,
            "name": "c",
            "context": ["背景"],
            "input": {"type": "single_turn", "prompt": "q"},
            "expected": {"tools": {"required": ["t1"]}},
        }
    )
    b = TraceBuilder()
    b.feed(
        TraceEvent(
            event_id="e1",
            trace_id="t",
            type="tool.call",
            timestamp="2026-09-23T11:00:00+08:00",
            data={"name": "t1"},
        )
    )
    trace = DeepEvalCapabilityAdapter().convert(
        case, b.build(), final_output="out", latency_ms=100, cost=0.25
    )
    assert trace["input"] == "q"
    assert trace["actual_output"] == "out"
    # PRD §37：tools_called ← trace tool calls，必须带参数（ArgumentCorrectness 靠它判）
    assert trace["tools_called"] == [{"name": "t1", "input_parameters": None}]
    assert trace["expected_tools"] == [{"name": "t1"}]
    # PRD §37：token_cost ← calculated cost，不是 token 数量
    assert trace["token_cost"] == 0.25
    assert trace["type"] == "llm"


def test_deepeval_convert_multi_turn_folds_prior_turns_into_context() -> None:
    """多轮 case 按"终局切片 + 前序轮次入 context"表达（不是退化成首问）。"""
    case = Case.model_validate(
        {
            "id": "m",
            "version": 1,
            "name": "m",
            "context": ["背景"],
            "input": {
                "type": "multi_turn",
                "turns": [{"user": "第一问"}, {"user": "第二问"}],
            },
        }
    )
    from agent_eval.trace.builder import TraceBuilder

    trace = DeepEvalCapabilityAdapter().convert(
        case,
        TraceBuilder().build(),
        final_output="终答",
        latency_ms=100,
        turn_outputs=["第一答"],
    )
    assert trace["type"] == "llm"  # SDK 六个 agent.* metric 只接 LLMTestCase
    assert trace["input"] == "第二问"  # 终局评测对象 = 最后一轮
    assert trace["actual_output"] == "终答"
    assert trace["context"] == ["背景", "user: 第一问", "assistant: 第一答"]


def test_deepeval_convert_does_not_use_context_as_expected_output() -> None:
    """回归：`expected_output` 曾是 ``case.context``（judge 背景），拿背景当标准答案。"""
    case = Case.model_validate(
        {
            "id": "c",
            "version": 1,
            "name": "c",
            "context": ["这是背景说明，不是标准答案"],
            "input": {"type": "single_turn", "prompt": "q"},
        }
    )
    from agent_eval.trace.builder import TraceBuilder

    trace = DeepEvalCapabilityAdapter().convert(
        case, TraceBuilder().build(), final_output="out", latency_ms=1
    )
    assert trace["expected_output"] is None
    assert trace["context"] == ["这是背景说明，不是标准答案"]


@pytest.mark.real_judge
def test_deepeval_evaluate_with_fake_module(monkeypatch) -> None:
    fake_metrics = types.ModuleType("deepeval.metrics")

    class TaskCompletionMetric:
        def __init__(self, threshold=None, **kwargs):
            self.threshold = threshold

        def measure(self, test_case):
            return 0.9

    fake_metrics.TaskCompletionMetric = TaskCompletionMetric
    fake_test_case = types.ModuleType("deepeval.test_case")
    fake_test_case.LLMTestCase = lambda **kw: {"fake": kw}
    fake_test_case.ToolCall = lambda **kw: {"tool": kw}
    fake_pkg = types.ModuleType("deepeval")
    fake_pkg.metrics = fake_metrics
    fake_pkg.test_case = fake_test_case
    monkeypatch.setitem(sys.modules, "deepeval", fake_pkg)
    monkeypatch.setitem(sys.modules, "deepeval.metrics", fake_metrics)
    monkeypatch.setitem(sys.modules, "deepeval.test_case", fake_test_case)

    adapter = DeepEvalCapabilityAdapter()
    assert adapter.probe()["agent.task_completion"] is True

    import asyncio

    score, reason = asyncio.run(
        adapter.evaluate("agent.task_completion", 0.7, {"type": "llm", "input": "q"})
    )
    assert score == 0.9
    assert "above" in reason


@pytest.mark.real_judge
def test_probe_rejects_incompatible_sdk_shape(monkeypatch) -> None:
    """回归：probe 曾只看"类是否存在"，SDK 形状变了仍全绿 → 运行期每 case 判死。

    这里模拟一个"六个 metric 类都在、但 test_case 构造形状对不上"的 SDK：
    ``ToolCall`` 缺席时 probe 必须整体 False，交给 §7.4 的 fallback 链降级。
    """
    fake_metrics = types.ModuleType("deepeval.metrics")
    for class_name in DEEPEVAL_AGENT_METRICS.values():
        setattr(fake_metrics, class_name, type(class_name, (), {}))
    fake_test_case = types.ModuleType("deepeval.test_case")
    fake_test_case.LLMTestCase = lambda **kw: kw  # 没有 ToolCall
    fake_pkg = types.ModuleType("deepeval")
    fake_pkg.metrics = fake_metrics
    fake_pkg.test_case = fake_test_case
    monkeypatch.setitem(sys.modules, "deepeval", fake_pkg)
    monkeypatch.setitem(sys.modules, "deepeval.metrics", fake_metrics)
    monkeypatch.setitem(sys.modules, "deepeval.test_case", fake_test_case)

    assert not any(DeepEvalCapabilityAdapter().probe().values())


@pytest.mark.real_judge
def test_build_test_case_against_real_sdk() -> None:
    """对**真实** 已安装 SDK 的构造冒烟。

    这是本轮修复的核心回归网：之前唯一走 evaluate 的用例用的是假 deepeval 模块
    （``LLMTestCase = lambda **kw``），对真实签名约束完全免疫，于是
    ``ConversationTestCase`` 类名错误与 ``tools_called`` 类型错误两个 bug
    在整个测试套件里都不可见。此处不 mock：装了 SDK 就用真 SDK 构造。
    """
    pytest.importorskip("deepeval")
    from agent_eval.models.events import TraceEvent
    from agent_eval.trace.builder import TraceBuilder

    adapter = DeepEvalCapabilityAdapter()
    assert all(adapter.probe().values()), "probe 必须能构造出真实 test case"

    case = Case.model_validate(
        {
            "id": "c",
            "version": 1,
            "name": "c",
            "context": ["背景"],
            "input": {"type": "single_turn", "prompt": "q"},
            "expected": {"tools": {"required": ["execute_sql"]}},
        }
    )
    builder = TraceBuilder()
    builder.feed(
        TraceEvent(
            event_id="e1",
            trace_id="t",
            type="tool.call",
            timestamp="2026-09-23T11:00:00+08:00",
            data={"name": "execute_sql", "arguments": {"sql": "select 1"}},
        )
    )
    trace = adapter.convert(case, builder.build(), final_output="out", latency_ms=100, cost=0.01)
    test_case = adapter._build_test_case(trace)
    assert test_case.tools_called[0].name == "execute_sql"
    assert test_case.tools_called[0].input_parameters == {"sql": "select 1"}
    assert test_case.token_cost == 0.01

    # 多轮 case 同样必须构造得出来（曾经因类名错误在此抛 EvaluationInfraError）
    multi = Case.model_validate(
        {
            "id": "m",
            "version": 1,
            "name": "m",
            "input": {"type": "multi_turn", "turns": [{"user": "一"}, {"user": "二"}]},
        }
    )
    multi_trace = adapter.convert(multi, TraceBuilder().build(), final_output="答", latency_ms=10)
    adapter._build_test_case(multi_trace)


def test_custom_namespace_fails_fast_instead_of_silent_drop() -> None:
    """PRD §42 的 ``custom.*``（用户 GEval）未实现，必须 fail-fast 而不是静默丢弃。

    回归背景：``provider_for('custom.x')`` 曾兜底成 ``native``，随后 runner 的分派
    既不入 judge 也不入 harness —— profile 里写 custom.* 什么都不会发生，连 skipped
    都不记。现在按 §7.4 在启动期报 MetricUnavailableError（exit 3）。
    """
    with pytest.raises(MetricUnavailableError, match="Custom GEval"):
        resolve_metric(MetricSpec(id="custom.database_answer_quality"), {}, no_judge=False)


def test_unknown_native_metric_fails_fast() -> None:
    """注册表里没有的 native.* id 是拼写错误，不得被当成"有一条 native 规则在评测"。"""
    with pytest.raises(MetricUnavailableError, match="not a registered platform metric"):
        resolve_metric(MetricSpec(id="native.output_check"), {}, no_judge=False)


def test_scan_unsupported_assertions() -> None:
    ok = Case.model_validate(
        {
            "id": "ok",
            "version": 1,
            "name": "ok",
            "input": {"type": "single_turn", "prompt": "q"},
            "expected": {"status": "success"},
        }
    )
    assert scan_unsupported_assertions([ok]) == []
    bad = Case.model_validate(
        {
            "id": "bad",
            "version": 1,
            "name": "b",
            "input": {"type": "single_turn", "prompt": "q"},
            "expected": {"pytest": {"tests/": {"exit_code": 0}}},
        }
    )
    found = scan_unsupported_assertions([bad])
    assert len(found) == 1 and found[0].startswith("bad: expected[expected].pytest:")
    assert "§88" in found[0]  # 报错要能指向下一步动作（依赖沙箱）


def test_scan_covers_all_three_mount_points() -> None:
    case = Case.model_validate(
        {
            "id": "multi",
            "version": 1,
            "name": "m",
            "input": {
                "type": "multi_turn",
                "turns": [
                    {"user": "t1", "expect": {"permission": {}}},
                    {"user": "t2"},
                ],
            },
            "expected": {"lint": {"ruff": {}}},
            "expected_final": {"git_diff": "HEAD"},
        }
    )
    problems = scan_unsupported_assertions([case])
    assert any("expected[expected].lint" in p for p in problems)
    assert any("expected[expected.final].git_diff" in p for p in problems)
    assert any("turns[0].expect.permission" in p for p in problems)
    assert len(problems) == 3


class TestVocabularyBoundary:
    """Spec §19 的验收口径：**已实现的不许再报"尚未实现"**，
    未实现的仍必须报错——两个方向都要守住，只守一侧等于把词汇表放宽成"什么都收"。
    """

    def test_shipped_dataset_has_no_unsupported_declarations(self) -> None:
        """仓库里的示例集必须全部可评测（否则 `benchmark run` 会 exit 3）。"""
        from pathlib import Path

        from agent_eval.loading.loader import load_benchmark, load_dataset

        evals = Path(__file__).resolve().parents[1] / "evals"
        benchmark = load_benchmark(evals, "database-core")
        _info, cases = load_dataset(evals, benchmark.dataset)
        found = scan_unsupported_assertions(cases)
        assert found == [], f"示例集里有无法评测的声明：{found}"

    @pytest.mark.parametrize(
        "key,needle",
        [
            ("pytest", "§88"),  # 执行型 → 指向沙箱
            ("build", "§88"),
            ("lint", "§88"),
            # 已裁决不做 → 指向裁决与替代手段（Spec §20.4：workdir 会继承外层仓库）
            ("git_diff", "Spec §20.4"),
        ],
    )
    def test_deferred_keys_still_fail_fast(self, key: str, needle: str) -> None:
        case = Case.model_validate(
            {
                "id": "d",
                "version": 1,
                "name": "d",
                "input": {"type": "single_turn", "prompt": "q"},
                "expected": {key: {"x": {}}},
            }
        )
        found = scan_unsupported_assertions([case])
        assert len(found) == 1, found
        assert needle in found[0], found[0]
        # 三分类必须彼此可区分：执行型不能说成"依赖别的任务"
        other = "Spec §20.4" if needle == "§88" else "§88"
        assert other not in found[0] or needle == other

    def test_git_diff_rejection_points_at_an_alternative(self) -> None:
        """`git_diff` 是三类里唯一的"裁决不做"：报错必须给出替代手段。

        Spec §20.4：fixture workdir 被 copytree 到平台仓库工作树内部，那里
        git diff 报的是**平台源码**的改动，agent 新建的文件反而不可见。
        替代手段是 `file_state` 快照比对。只说"尚未实现"会让下一个人
        重新做一遍这个判断。
        """
        case = Case.model_validate(
            {
                "id": "d",
                "version": 1,
                "name": "d",
                "input": {"type": "single_turn", "prompt": "q"},
                "expected": {"git_diff": {"x": {}}},
            }
        )
        found = scan_unsupported_assertions([case])
        assert len(found) == 1, found
        assert "file_state" in found[0], found[0]

    def test_implemented_keys_are_no_longer_reported(self) -> None:
        """本轮实现的键必须从"尚未实现"里消失，否则验收口径没达成。"""
        case = Case.model_validate(
            {
                "id": "impl",
                "version": 1,
                "name": "i",
                "input": {"type": "single_turn", "prompt": "q"},
                "expected": {
                    "exit_code": 0,
                    "database_state": {"tables_absent": ["audit_log"]},
                    "file_state": {"absent": ["tmp/x"]},
                    "sql_result": {"min_rows": 1},
                    "constraints": {"max_cost": 0.05},
                },
            }
        )
        assert scan_unsupported_assertions([case]) == []

    def test_permission_is_reported_as_outside_vocabulary(self) -> None:
        """`permission` 已移除（Spec §19.7）：提示必须指向词汇表，而不是"尚未实现"。"""
        case = Case.model_validate(
            {
                "id": "perm",
                "version": 1,
                "name": "p",
                "input": {"type": "single_turn", "prompt": "q"},
                "expected": {"permission": {"allow": ["read"]}},
            }
        )
        found = scan_unsupported_assertions([case])
        assert len(found) == 1
        assert "不在断言词汇表内" in found[0]
