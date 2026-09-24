"""Metric Registry 解析（§7.4）与 DeepEvalCapabilityAdapter 测试。"""

from __future__ import annotations

import sys
import types

import pytest

from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter
from agent_eval.evaluators.registry import (
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
        case, b.build(), final_output="out", latency_ms=100, tokens=42
    )
    assert trace["input"] == "q"
    assert trace["actual_output"] == "out"
    assert trace["tools_called"] == ["t1"]
    assert trace["expected_tools"] == ["t1"]
    assert trace["token_cost"] == 42
    assert trace["type"] == "llm"


@pytest.mark.real_judge
def test_deepeval_evaluate_with_fake_module(monkeypatch) -> None:
    fake_metrics = types.ModuleType("deepeval.metrics")

    class TaskCompletionMetric:
        def __init__(self, threshold=None):
            self.threshold = threshold

        def measure(self, test_case):
            return 0.9

    fake_metrics.TaskCompletionMetric = TaskCompletionMetric
    fake_test_case = types.ModuleType("deepeval.test_case")
    fake_test_case.LLMTestCase = lambda **kw: {"fake": kw}
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
            ("git_diff", "Spec §19.1"),  # 依赖其他观测面 → 指向处置表
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
        other = "Spec §19.1" if needle == "§88" else "§88"
        assert other not in found[0] or needle == other

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
