"""Regression compare 的方向语义（PRD §53/§55/§105）。

核心约束：token / tool_calls / latency / cost 这类「资源消耗」metric 数值上升不是进步，
必须判为 regressed；只有质量类 metric 才是越大越好。
"""

from __future__ import annotations

from agent_eval.models.results import CaseRunResult, CaseStatus
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.regression.compare import (
    DEFAULT_PERFORMANCE_THRESHOLDS,
    _case_performance,
    _lower_is_better,
    _metric_diffs,
    compare_runs,
)


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

    def test_wall_clock_floor_covers_both_noise_shapes(self) -> None:
        """两种噪声形状都要拦（回归断言：联调实测的间歇 flake）。

        实测 flake：`test_api.py::test_regression_between_two_runs` 间歇断言失败
        （``{'improved','unchanged'}``）。机理是 5 个 case 全 1ms 的那一侧均值恰好
        1.0ms，而旧判据 ``max(base, cand) < 1.0`` 的等号不成立——转去比相对阈值，
        1.0 ↔ 0.6 于是判 ``improved``（-40% > 20%）。时间戳的量化步长就是 1ms，
        所以"差值 ≤ 分辨率"无信号（形状一）、"任一侧低于分辨率"也无信号
        （形状二：0.4 ↔ 5ms 的相对变化是 ±1000%，近 0 基线只会放大它）。
        """
        for base, cand in ((1.0, 0.6), (1.0, 0.0), (0.0, 1.0), (1.0, 2.0), (2.0, 1.0)):
            diffs = _metric_diffs({"latency_ms": base}, {"latency_ms": cand})
            assert [d.verdict for d in diffs] == ["unchanged"], (base, cand)
        for base, cand in ((0.4, 5.0), (5.0, 0.4), (0.0, 8.0)):
            diffs = _metric_diffs({"latency_ms": base}, {"latency_ms": cand})
            assert [d.verdict for d in diffs] == ["unchanged"], (base, cand)
        # 两侧都可测、差值远超分辨率：照判方向（否则这个下限会把真回归也吃掉）
        for base, cand, verdict in ((300.0, 400.0, "regressed"), (400.0, 300.0, "improved")):
            diffs = _metric_diffs({"latency_ms": base}, {"latency_ms": cand})
            assert [d.verdict for d in diffs] == [verdict], (base, cand)

    def test_case_performance_latency_has_the_same_absolute_floor(self) -> None:
        """case 级性能回归此前**完全没有**绝对下限：1ms ↔ 2ms 判退步并进 Gate。

        这条 diff 会进 ``comparison.performance_regressions``（Gate 输入），
        所以同一份墙钟噪声在 metric 段被降噪、在 case 段却触发 Gate，是同一种
        错误的两种表现。真实量级（300 → 400ms）不受影响。
        """
        near = _case_performance(
            [_result("b", "c1", latency_ms=1)],
            [_result("c", "c1", latency_ms=2)],
            DEFAULT_PERFORMANCE_THRESHOLDS,
        )
        assert not any(p.regressed for p in near), [(p.metric, p.delta_percent) for p in near]
        real = _case_performance(
            [_result("b", "c1", latency_ms=300)],
            [_result("c", "c1", latency_ms=400)],
            DEFAULT_PERFORMANCE_THRESHOLDS,
        )
        assert next(p for p in real if p.metric == "latency_ms").regressed is True
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

    def test_endpoint_kind_mismatch_is_invalid(self) -> None:
        """联调实测（2026-09-30）：内置 mock 的 run 不能给真实 SUT 的 run 当基线。

        本机先跑 `fake://` 冒烟再第一次接真实 SUT 是常见顺序，两个 run 在 dataset /
        套件 / 模型上都可以一致，唯一不成立的是"同一个被测对象"——于是"native.output_checks
        回归 -100%"这种纯由换 SUT 造成的数字被当成回归信号摆上台面。
        """
        comparison = compare_runs(
            _meta("base", agent_endpoint="fake://"),
            [_result("base", "c1")],
            _meta("cand", agent_endpoint="http://127.0.0.1:8901"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is False
        assert "endpoint kind" in (comparison.invalid_reason or "")

    def test_same_endpoint_kind_on_different_hosts_is_valid(self) -> None:
        """粒度是接入类型而非整条 URL：同一个 SUT 在开发机与 CI 上 host/port 必然不同，
        钉死 URL 会让跨机基线永远不可比（模型漂移另有 agent_model 守卫覆盖）。"""
        comparison = compare_runs(
            _meta("base", agent_endpoint="http://127.0.0.1:8901"),
            [_result("base", "c1")],
            _meta("cand", agent_endpoint="http://ci-runner.internal:9999"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is True

    def test_endpoint_unset_on_both_sides_is_valid(self) -> None:
        """旧 run（未记录 endpoint，空串）相互比较仍有效——与 suites_covered 同理。"""
        comparison = compare_runs(
            _meta("base"),
            [_result("base", "c1")],
            _meta("cand"),
            [_result("cand", "c1")],
        )
        assert comparison.valid is True

    def test_multiple_incomparable_reasons_are_all_reported(self) -> None:
        """一次 run 同时踩中两条时两条都要在——早先是 last-wins 赋值，前一条会消失。

        两条原因指向不同的修法（换 dataset / 换套件 / 换模型 / 换基线来源），
        只报一条等于把另一条排查线索藏起来。
        """
        comparison = compare_runs(
            _meta("base", dataset_version="v1", suites_covered={"golden": 24}),
            [_result("base", "c1")],
            _meta("cand", dataset_version="v2", suites_covered={"smoke": 3}),
            [_result("cand", "c1")],
        )
        assert comparison.valid is False
        reason = comparison.invalid_reason or ""
        assert "dataset_version" in reason
        assert "suites_covered" in reason
