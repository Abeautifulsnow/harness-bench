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

    def test_more_tokens_is_a_regression_not_an_improvement(self) -> None:
        diffs = _metric_diffs({"tokens": 240.0}, {"tokens": 266.6667})
        assert [d.verdict for d in diffs] == ["regressed"]

    def test_fewer_tokens_is_an_improvement(self) -> None:
        diffs = _metric_diffs({"tokens": 240.0}, {"tokens": 200.0})
        assert [d.verdict for d in diffs] == ["improved"]

    def test_higher_task_success_is_an_improvement(self) -> None:
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
