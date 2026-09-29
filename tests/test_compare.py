"""Regression compare 的方向语义（PRD §53/§55/§105）。

核心约束：token / tool_calls / latency / cost 这类「资源消耗」metric 数值上升不是进步，
必须判为 regressed；只有质量类 metric 才是越大越好。
"""

from __future__ import annotations

from agent_eval.models.results import CaseRunResult, CaseStatus
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.regression.compare import _lower_is_better, _metric_diffs, compare_runs


def _meta(run_id: str, **overrides) -> RunMetadata:
    payload = {
        "run_id": run_id,
        "benchmark_id": "b1",
        "dataset_id": "d1",
        "dataset_version": "v1",
        "dataset_hash": "h1",
        "profile": "mock",
        "status": RunStatus.completed,
    }
    payload.update(overrides)
    return RunMetadata(**payload)


def _result(run_id: str, case_id: str, **overrides) -> CaseRunResult:
    payload = {
        "id": f"{run_id}-{case_id}",
        "run_id": run_id,
        "case_id": case_id,
        "case_version": 1,
        "iteration": 1,
        "status": CaseStatus.PASS,
    }
    payload.update(overrides)
    return CaseRunResult(**payload)


class TestDirectionSemantics:
    def test_resource_metrics_are_lower_is_better(self) -> None:
        for metric in ("tokens", "tool_calls", "latency_ms", "cost", "task_failure"):
            assert _lower_is_better(metric), metric

    def test_quality_metrics_are_higher_is_better(self) -> None:
        for metric in ("task_success", "native.status", "agent.task_completion"):
            assert not _lower_is_better(metric), metric

    def test_more_tokens_beyond_threshold_is_a_regression(self) -> None:
        # +37.5% 超出 tokens 的噪声下限（25%，与 case 级性能回归同源）
        diffs = _metric_diffs({"tokens": 240.0}, {"tokens": 330.0})
        assert [d.verdict for d in diffs] == ["regressed"]

    def test_fewer_tokens_beyond_threshold_is_an_improvement(self) -> None:
        diffs = _metric_diffs({"tokens": 240.0}, {"tokens": 150.0})
        assert [d.verdict for d in diffs] == ["improved"]

    def test_resource_change_within_threshold_is_unchanged(self) -> None:
        """ROADMAP 发现的 1：方向是信号不是算术，阈值内的资源变化不给方向。

        +11.1% 的 token 波动在 case 级性能回归的 25% 阈值内本来就是 unchanged；
        display 方向与 gate 阈值同源，否则同一份报告两处口径。
        """
        diffs = _metric_diffs({"tokens": 240.0}, {"tokens": 266.6667})
        assert [d.verdict for d in diffs] == ["unchanged"]
        latency = _metric_diffs({"latency_ms": 100.0}, {"latency_ms": 115.0})
        assert [d.verdict for d in latency] == ["unchanged"]  # +15% < latency 20%

    def test_wall_clock_below_millisecond_floor_is_unchanged(self) -> None:
        """毫秒以下的 wall-clock 均值没有任何信号：调度抖动就足以让它翻倍/归零。

        实测 flake：同一 mock 连跑 latency_ms 均值 0.4 ↔ 0，相对变化 ±100%，
        相对阈值在 0 基线/近 0 基线上拦不住，只能用绝对判据（< 1ms 判 unchanged）。
        """
        for base, cand in ((0.4, 0.0), (0.0, 0.4), (0.4, 0.8)):
            diffs = _metric_diffs({"latency_ms": base}, {"latency_ms": cand})
            assert [d.verdict for d in diffs] == ["unchanged"], (base, cand)

    def test_quality_direction_has_no_noise_floor(self) -> None:
        """质量类 metric 的方向不需要下限：得分是确定性的，任何变化都是信号。"""
        diffs = _metric_diffs({"task_success": 0.5}, {"task_success": 0.75})
        assert [d.verdict for d in diffs] == ["improved"]

    def test_identical_value_is_unchanged(self) -> None:
        diffs = _metric_diffs({"tokens": 240.0}, {"tokens": 240.0})
        assert [d.verdict for d in diffs] == ["unchanged"]

    def test_metric_present_on_one_side_only_is_skipped(self) -> None:
        assert _metric_diffs({"tokens": 1.0}, {"cost": 2.0}) == []


class TestComparisonValidity:
    def test_dataset_version_mismatch_is_invalid(self) -> None:
        comparison = compare_runs(
            _meta("base", dataset_version="v1"),
            [_result("base", "c1")],
            _meta("cand", dataset_version="v2"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is False
        assert "dataset_version" in (comparison.invalid_reason or "")

    def test_benchmark_mismatch_is_invalid(self) -> None:
        comparison = compare_runs(
            _meta("base", benchmark_id="a"),
            [_result("base", "c1")],
            _meta("cand", benchmark_id="b"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is False
        assert "benchmark" in (comparison.invalid_reason or "")

    def test_matching_runs_are_valid(self) -> None:
        comparison = compare_runs(
            _meta("base"),
            [_result("base", "c1")],
            _meta("cand"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is True
        assert comparison.invalid_reason is None

    def test_suite_composition_mismatch_is_invalid(self) -> None:
        """ROADMAP 发现的 2：均值定义在各自的 case 集合上，集合不同不可比。"""
        comparison = compare_runs(
            _meta("base", suites_covered={"golden": 24}),
            [_result("base", "c1")],
            _meta("cand", suites_covered={"smoke": 3}),
            [_result("cand", "c1")],
        )
        assert comparison.valid is False
        assert "suites_covered" in (comparison.invalid_reason or "")

    def test_empty_composition_on_both_sides_is_valid(self) -> None:
        """旧 run（未记录 suites_covered）相互比较仍有效，不受新约束影响。"""
        comparison = compare_runs(
            _meta("base", suites_covered={}),
            [_result("base", "c1")],
            _meta("cand", suites_covered={}),
            [_result("cand", "c1")],
        )
        assert comparison.valid is True

    def test_agent_model_mismatch_is_invalid(self) -> None:
        """A4：模型漂移静默污染基线——token/延迟/通过率全变却归因为"回归"。"""
        comparison = compare_runs(
            _meta("base", agent_model="model-a"),
            [_result("base", "c1")],
            _meta("cand", agent_model="model-b"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is False
        assert "agent model" in (comparison.invalid_reason or "")

    def test_agent_model_unset_on_both_sides_is_valid(self) -> None:
        """双侧均未记录（旧 run）无法证伪可比性，不拦——与 suites_covered 同理。"""
        comparison = compare_runs(
            _meta("base"),
            [_result("base", "c1")],
            _meta("cand"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is True

    def test_token_usage_scope_mismatch_is_invalid(self) -> None:
        """A3 修订五：口径不同的用量不可比——"只看到输入"的 run 与"看到全部"
        的 run 作差，差值里混着口径变化，正是 §4.3 要禁的静默不可比。"""
        comparison = compare_runs(
            _meta("base", token_usage_scope="full"),
            [_result("base", "c1")],
            _meta("cand", token_usage_scope="partial"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is False
        assert "usage scope" in (comparison.invalid_reason or "")

    def test_token_usage_scope_unset_on_both_sides_is_valid(self) -> None:
        comparison = compare_runs(
            _meta("base"),
            [_result("base", "c1")],
            _meta("cand"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is True
