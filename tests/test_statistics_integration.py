"""统计置信接线的集成测试（Spec §26.3/§26.4/§26.5）。

三个契约：
1. report 三产物（report.json / summary.md / report.html）的 CI 值同源一致；
2. compare 统计块只进呈现层——``within_noise`` 与 ``REGRESSION`` 并存（互不覆盖）；
3. criteria_version 双侧声明且不等 → INVALID；单侧缺失不拦（历史 run 不断基线）。

第四契约（Spec §26.5 自动指纹）：judge_config_digest 双侧都有且不等 → INVALID
（理由指名两侧值与排查方向）；任一侧 None（旧 run / no_judge run）→ 放行。
"""

from __future__ import annotations

from pathlib import Path

from agent_eval.loading.loader import load_profile
from agent_eval.models.regression import GateReport
from agent_eval.models.results import CaseRunResult, CaseStatus, MetricResultModel
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.regression.compare import compare_runs
from agent_eval.reports.aggregate import build_aggregate
from agent_eval.reports.html import render_html
from agent_eval.reports.report import build_report, render_summary


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


def _result(
    run_id: str,
    case_id: str,
    statuses: list[CaseStatus],
    *,
    case_version: int = 1,
) -> list[CaseRunResult]:
    """一个 case 的多 iteration 结果；status 决定 stability（Spec §3）。"""
    return [
        CaseRunResult(
            id=f"{run_id}-{case_id}-i{i}",
            run_id=run_id,
            case_id=case_id,
            case_version=case_version,
            iteration=i,
            status=status,
        )
        for i, status in enumerate(statuses, 1)
    ]


class TestReportConfidence:
    def _aggregate(self):
        results = _result("r1", "c1", [CaseStatus.PASS] * 3)
        return build_aggregate(_meta("r1"), results)

    def test_case_level_ci_in_report_dict(self) -> None:
        aggregate = self._aggregate()
        payload = build_report(aggregate)
        case = payload["cases"][0]
        # wilson(3,3) 的下界必须远小于 1：3/3 ≠ 确定 100%（Spec §26.3 的存在理由）
        assert case["pass_rate_ci95"] is not None
        lo, hi = case["pass_rate_ci95"]
        assert lo < 1.0 and hi == 1.0

    def test_ci_consistent_across_three_artifacts(self) -> None:
        aggregate = self._aggregate()
        payload = build_report(aggregate)
        gate = GateReport(gate="pr", run_id="r1")
        summary = render_summary(payload, gate)
        html = render_html(aggregate, gate)
        case_ci = payload["cases"][0]["pass_rate_ci95"]
        run_ci = payload["pass_rate_ci95"]
        # 同源：case 级与 run 级用同一份 wilson 输入（3 pass / 3 valid）
        assert case_ci == run_ci
        # summary.md 与 report.html 各自渲染同一组数字
        assert f"95% CI {case_ci[0]:.0%}" in summary
        lo_pct = f"{case_ci[0] * 100:.1f}"
        assert f"pass rate 95% CI [{lo_pct}" in html

    def test_no_verdict_iterations_omit_ci(self) -> None:
        from agent_eval.models.results import CaseStatus as Status

        results = _result("r1", "c1", [Status.ERROR])
        payload = build_report(build_aggregate(_meta("r1"), results))
        assert payload["pass_rate_ci95"] is None
        assert payload["cases"][0]["pass_rate_ci95"] is None
        summary = render_summary(payload, GateReport(gate="pr", run_id="r1"))
        assert "95% CI" not in summary


class TestComparisonStatistics:
    def test_paired_counts_exclusions_and_noise_verdict(self) -> None:
        base = [
            *_result("b", "reg", [CaseStatus.PASS] * 3),
            *_result("b", "imp", [CaseStatus.FAIL] * 3),
            *_result("b", "same", [CaseStatus.PASS] * 3),
            *_result("b", "flk", [CaseStatus.PASS, CaseStatus.FAIL, CaseStatus.PASS]),
            *_result("b", "unk", [CaseStatus.PASS, CaseStatus.FAIL]),
            *_result("b", "inv", [CaseStatus.PASS] * 3, case_version=2),
        ]
        cand = [
            *_result("c", "reg", [CaseStatus.FAIL] * 3),
            *_result("c", "imp", [CaseStatus.PASS] * 3),
            *_result("c", "same", [CaseStatus.PASS] * 3),
            *_result("c", "flk", [CaseStatus.PASS, CaseStatus.FAIL, CaseStatus.PASS]),
            *_result("c", "unk", [CaseStatus.PASS, CaseStatus.FAIL]),
            *_result("c", "inv", [CaseStatus.PASS] * 3),
        ]
        comparison = compare_runs(_meta("b"), base, _meta("c"), cand)
        stats = comparison.statistics
        assert stats is not None
        assert stats.paired_cases == 3
        assert stats.regressions == 1  # reg: STABLE_PASS → STABLE_FAIL
        assert stats.improvements == 1  # imp: STABLE_FAIL → STABLE_PASS
        assert stats.excluded_flaky == 1
        assert stats.excluded_unknown == 1
        assert stats.excluded_invalid == 1
        # b=1, c=1 → p=1.0：这组翻转完全可能是噪声
        assert stats.mcnemar_p == 1.0
        assert stats.verdict == "within_noise"

    def test_statistics_never_override_regression_state(self) -> None:
        """裁决 §26.4 的钉子：统计说 within_noise，REGRESSION 照旧是 REGRESSION。"""
        base = [*_result("b", "one", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "one", [CaseStatus.FAIL] * 3)]
        comparison = compare_runs(_meta("b"), base, _meta("c"), cand)
        assert comparison.cases[0].state.value == "REGRESSION"
        assert comparison.statistics is not None
        assert comparison.statistics.verdict == "within_noise"
        assert comparison.valid is True

    def test_clear_signal_is_significant(self) -> None:
        base = []
        cand = []
        for i in range(7):
            base += _result("b", f"c{i}", [CaseStatus.PASS] * 3)
            cand += _result("c", f"c{i}", [CaseStatus.FAIL] * 3)
        comparison = compare_runs(_meta("b"), base, _meta("c"), cand)
        stats = comparison.statistics
        assert stats is not None
        # b=7, c=0 → p = 2/128 ≈ 0.0156：单方向 7 连翻不是噪声
        assert stats.mcnemar_p == 0.015625
        assert stats.verdict == "significant"

    def test_invalid_comparison_has_no_statistics_override(self) -> None:
        base = [*_result("b", "c1", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "c1", [CaseStatus.PASS] * 3)]
        comparison = compare_runs(
            _meta("b", dataset_version="v1"),
            base,
            _meta("c", dataset_version="v2"),
            cand,
        )
        assert comparison.valid is False
        # 统计块照常计算（呈现层事实），但 valid / 判定不受它影响
        assert comparison.statistics is not None


class TestCriteriaVersionGuard:
    def test_mismatch_is_invalid(self) -> None:
        base = [*_result("b", "c1", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "c1", [CaseStatus.PASS] * 3)]
        comparison = compare_runs(
            _meta("b", criteria_version="rubric-v1"),
            base,
            _meta("c", criteria_version="rubric-v2"),
            cand,
        )
        assert comparison.valid is False
        assert comparison.invalid_reason is not None
        assert "criteria_version" in comparison.invalid_reason

    def test_missing_on_one_side_does_not_block_legacy_baselines(self) -> None:
        base = [*_result("b", "c1", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "c1", [CaseStatus.PASS] * 3)]
        comparison = compare_runs(_meta("b"), base, _meta("c", criteria_version="rubric-v1"), cand)
        assert comparison.valid is True

    def test_equal_versions_are_comparable(self) -> None:
        base = [*_result("b", "c1", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "c1", [CaseStatus.PASS] * 3)]
        comparison = compare_runs(
            _meta("b", criteria_version="rubric-v1"),
            base,
            _meta("c", criteria_version="rubric-v1"),
            cand,
        )
        assert comparison.valid is True

    def test_profile_yaml_parses_criteria_version(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "cv.yaml").write_text(
            "name: cv\ncriteria_version: rubric-v7\nmetrics: []\n", encoding="utf-8"
        )
        profile = load_profile(tmp_path, "cv")
        assert profile.criteria_version == "rubric-v7"

    def test_profile_without_criteria_version_defaults_none(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "plain.yaml").write_text("name: plain\nmetrics: []\n", encoding="utf-8")
        assert load_profile(tmp_path, "plain").criteria_version is None


class TestJudgeConfigDigestGuard:
    """Spec §26.5 自动指纹守卫：criteria_version 未声明时兜住 threshold / 模型 / SDK 漂移。"""

    def test_mismatch_is_invalid_and_names_both_sides(self) -> None:
        base = [*_result("b", "c1", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "c1", [CaseStatus.PASS] * 3)]
        comparison = compare_runs(
            _meta("b", judge_config_digest="aaaa1111bbbb2222"),
            base,
            _meta("c", judge_config_digest="cccc3333dddd4444"),
            cand,
        )
        assert comparison.valid is False
        reason = comparison.invalid_reason or ""
        assert "judge_config_digest" in reason
        assert "aaaa1111bbbb2222" in reason
        assert "cccc3333dddd4444" in reason
        # 理由指名排查方向（PRD A3）：对照哪些字段排查哪一项变了
        for hint in ("judge_model", "criteria_version", "deepeval_version"):
            assert hint in reason

    def test_digest_none_on_one_side_is_valid(self) -> None:
        """旧 run（无指纹）/ no_judge run 单侧 None 放行——与 criteria_version 守卫同取舍。"""
        base = [*_result("b", "c1", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "c1", [CaseStatus.PASS] * 3)]
        comparison = compare_runs(
            _meta("b"),
            base,
            _meta("c", judge_config_digest="aaaa1111bbbb2222"),
            cand,
        )
        assert comparison.valid is True

    def test_equal_digests_are_comparable(self) -> None:
        base = [*_result("b", "c1", [CaseStatus.PASS] * 3)]
        cand = [*_result("c", "c1", [CaseStatus.PASS] * 3)]
        comparison = compare_runs(
            _meta("b", judge_config_digest="aaaa1111bbbb2222"),
            base,
            _meta("c", judge_config_digest="aaaa1111bbbb2222"),
            cand,
        )
        assert comparison.valid is True


class TestMetricResultShape:
    def test_metric_result_verdict_domain_unchanged(self) -> None:
        # 校准消费的判定事实以 runner 的构造为准（score >= threshold → pass）
        metric = MetricResultModel(
            id="m1",
            case_run_id="cr1",
            metric="agent.x",
            evaluator="deepeval",
            score=0.7,
            threshold=0.7,
            verdict="pass",
        )
        assert metric.verdict == "pass"
