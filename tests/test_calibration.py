"""Judge Calibration 的单元与 CLI 全链路测试（Spec §26）。

标注链路：create（抽样）→ import-labels / label → evaluate（快照）。
judge 隔离：校准只读已落盘的判定事实，不触发 judge，无需 deepeval。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
import yaml
from typer.testing import CliRunner

from agent_eval.calibration.agreement import compute_metric_agreement
from agent_eval.calibration.sample import (
    collect_production_items,
    collect_run_items,
    stratified_sample,
)
from agent_eval.calibration.store import CalibrationStore
from agent_eval.cli.app import app
from agent_eval.errors import InvalidCallError
from agent_eval.evaluators.fingerprint import (
    compute_eval_policy_digest,
    compute_judge_config_digest,
)
from agent_eval.evaluators.online_eval import (
    EvalPair,
    EvalPolicy,
    EvalPolicyMetric,
    align_policy_rows,
    policy_digest,
    run_online_evaluation,
)
from agent_eval.models.calibration import (
    DISAGREEMENT_CAUSES,
    HUMAN_VERDICTS,
    CalibrationItem,
    CalibrationSet,
)
from agent_eval.models.profile import MetricSpec
from agent_eval.models.results import CaseRunResult, MetricResultModel
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.runner.runner import RunConfig, Runner
from agent_eval.storage.production_store import ProductionStore
from agent_eval.storage.run_store import RunStore

runner = CliRunner()


def _meta(run_id: str, **overrides) -> RunMetadata:
    payload = {
        "run_id": run_id,
        "benchmark_id": "b1",
        "dataset_id": "d1",
        "dataset_version": "v1",
        "dataset_hash": "h1",
        "profile": "mock",
        "status": RunStatus.completed,
        "judge_model": "judge-x",
    }
    payload.update(overrides)
    return RunMetadata(**payload)


def _metric(
    metric: str, score: float, threshold: float, verdict: str, mid: str
) -> MetricResultModel:
    return MetricResultModel(
        id=mid,
        case_run_id=f"cr-{mid}",
        metric=metric,
        evaluator="deepeval",
        score=score,
        threshold=threshold,
        verdict=verdict,  # type: ignore[arg-type]
    )


def _result(
    run_id: str,
    case_id: str,
    iteration: int = 1,
    metric_results: list[MetricResultModel] | None = None,
) -> CaseRunResult:
    return CaseRunResult(
        id=f"{run_id}-{case_id}-i{iteration}",
        run_id=run_id,
        case_id=case_id,
        case_version=1,
        iteration=iteration,
        metric_results=metric_results or [],
    )


def _item(
    item_id: str,
    metric: str,
    *,
    judge: str = "pass",
    human: str | None = None,
    human2: str | None = None,
    rater: str = "",
    rater2: str = "",
    cause: str | None = None,
    score: float = 0.9,
    threshold: float = 0.7,
) -> CalibrationItem:
    return CalibrationItem(
        item_id=item_id,
        source="run",
        source_id="r1",
        case_id=f"c-{item_id}",
        metric=metric,
        score=score,
        threshold=threshold,
        judge_verdict=judge,  # type: ignore[arg-type]
        near_threshold=abs(score - threshold) <= 0.05,
        human_verdict=human,  # type: ignore[arg-type]
        human_verdict_2=human2,  # type: ignore[arg-type]
        human_rater=rater,
        human_rater_2=rater2,
        disagreement_cause=cause,  # type: ignore[arg-type]
    )


def _seed_run(data_dir: Path, run_id: str, *, items_per_case: int = 3, **meta_overrides) -> None:
    """落一个含 2 case × 3 iteration × 每轮 2 条 agent.* 判定的 run。"""
    store = RunStore(data_dir / "runs")
    store.create_run(_meta(run_id, **meta_overrides))
    for case in ("case-a", "case-b"):
        for iteration in range(1, items_per_case + 1):
            results = [
                _metric("agent.task_completion", 0.9, 0.7, "pass", f"{case}-{iteration}-tc"),
                _metric("agent.tool_correctness", 0.5, 0.8, "fail", f"{case}-{iteration}-tool"),
            ]
            store.save_case_run(_result(run_id, case, iteration, results))


class TestCollectItems:
    def test_run_items_filter_and_exclusion_counts(self) -> None:
        results = [
            _result(
                "r1",
                "c1",
                metric_results=[
                    _metric("agent.task_completion", 0.9, 0.7, "pass", "m1"),
                    _metric("agent.tool_correctness", 0.5, 0.8, "fail", "m2"),
                    _metric("agent.plan_quality", None, 0.7, "skipped", "m3"),
                    _metric("agent.plan_adherence", None, 0.7, "error", "m4"),
                    _metric("agent.step_efficiency", None, None, "warning", "m5"),
                    _metric("native.output_checks", 1.0, 1.0, "pass", "m6"),
                ],
            )
        ]
        items, excluded = collect_run_items(_meta("r1"), results)
        assert sorted(i.metric for i in items) == [
            "agent.task_completion",
            "agent.tool_correctness",
        ]
        assert excluded == {"skipped": 1, "error": 1, "warning": 1}

    def test_item_id_is_deterministic_per_source_fact(self) -> None:
        results = [
            _result(
                "r1",
                "c1",
                metric_results=[_metric("agent.task_completion", 0.9, 0.7, "pass", "m1")],
            )
        ]
        first, _ = collect_run_items(_meta("r1"), results)
        second, _ = collect_run_items(_meta("r1"), results)
        assert first[0].item_id == second[0].item_id

    def test_production_rows_map_to_items(self) -> None:
        evaluations = [
            {
                "evaluation_id": "eval_1",
                "rows": [
                    {
                        "case_id": "c1",
                        "metric": "agent.task_completion",
                        "threshold": 0.7,
                        "score": 0.9,
                        "verdict": "pass",
                    },
                    {
                        "case_id": "c2",
                        "metric": "agent.tool_correctness",
                        "threshold": 0.8,
                        "score": 0.4,
                        "verdict": "fail",
                    },
                    {
                        "case_id": "c3",
                        "metric": "agent.plan_quality",
                        "threshold": 0.7,
                        "score": None,
                        "verdict": "skipped",
                    },
                ],
            }
        ]
        items, excluded = collect_production_items("tr_x", evaluations)
        assert len(items) == 2
        assert items[0].sub_id == "eval_1"
        assert items[0].iteration is None
        assert excluded == {"skipped": 1}

    def test_near_threshold_flag_uses_review_margin(self) -> None:
        results = [
            _result(
                "r1",
                "c1",
                metric_results=[
                    _metric("agent.task_completion", 0.72, 0.7, "pass", "m1"),
                    _metric("agent.tool_correctness", 0.2, 0.8, "fail", "m2"),
                ],
            )
        ]
        items, _ = collect_run_items(_meta("r1"), results)
        by_metric = {i.metric: i for i in items}
        assert by_metric["agent.task_completion"].near_threshold is True
        assert by_metric["agent.tool_correctness"].near_threshold is False

    def test_production_warning_rows_counted_as_excluded(self) -> None:
        """与 run 源对称：warning verdict 计入剔除，而不是让 pydantic 崩溃。"""
        evaluations = [
            {
                "evaluation_id": "eval_1",
                "rows": [
                    {
                        "case_id": "c1",
                        "metric": "agent.task_completion",
                        "threshold": 0.7,
                        "score": 0.9,
                        "verdict": "pass",
                    },
                    {
                        "case_id": "c2",
                        "metric": "agent.tool_correctness",
                        "threshold": 0.8,
                        "score": 0.4,
                        "verdict": "warning",
                    },
                ],
            }
        ]
        items, excluded = collect_production_items("tr_x", evaluations)
        assert len(items) == 1
        assert excluded == {"warning": 1}


class TestStratifiedSample:
    def _population(self, total_per_metric: int) -> list[CalibrationItem]:
        items: list[CalibrationItem] = []
        for metric in ("agent.a", "agent.b", "agent.c"):
            items.extend(_item(f"{metric}-{i}", metric) for i in range(total_per_metric))
        return items

    def test_same_seed_same_sample(self) -> None:
        population = self._population(30)
        assert stratified_sample(population, 15, 7) == stratified_sample(population, 15, 7)

    def test_stratified_across_metrics_with_exact_total(self) -> None:
        sample = stratified_sample(self._population(30), 15, 7)
        assert len(sample) == 15
        counts: dict[str, int] = {}
        for item in sample:
            counts[item.metric] = counts.get(item.metric, 0) + 1
        # 三个 metric 等量 → 配额必须完全均分（最大余数法在整除时无余数）
        assert set(counts.values()) == {5}

    def test_n_ge_total_returns_everything(self) -> None:
        population = self._population(5)
        assert len(stratified_sample(population, 99, 1)) == 15

    def test_rejects_non_positive_n(self) -> None:
        try:
            stratified_sample(self._population(3), 0, 1)
        except InvalidCallError:
            return
        raise AssertionError("n=0 must raise InvalidCallError")

    def test_rejects_empty_population(self) -> None:
        try:
            stratified_sample([], 5, 1)
        except InvalidCallError:
            return
        raise AssertionError("empty population must raise InvalidCallError")


class TestJudgeConfigDigest:
    """judge 配置指纹（Spec §26.5 自动补充面）：确定性是"同口径"判定的前提。"""

    @staticmethod
    def _specs(threshold: float = 0.7) -> list[MetricSpec]:
        return [
            MetricSpec(id="agent.task_completion", threshold=threshold),
            MetricSpec(id="agent.tool_correctness", threshold=0.8),
        ]

    def _digest(self, **overrides: object) -> str | None:
        kwargs: dict[str, object] = {
            "criteria_version": "v1",
            "judge_model": "judge-x",
            "deepeval_version": "1.0.0",
            "judge_specs": self._specs(),
        }
        kwargs.update(overrides)
        return compute_judge_config_digest(**kwargs)  # type: ignore[arg-type]

    def test_same_config_same_digest_and_shape(self) -> None:
        assert self._digest() == self._digest()
        digest = self._digest()
        assert digest is not None
        assert len(digest) == 16

    def test_any_config_change_changes_digest(self) -> None:
        base = self._digest()
        assert base is not None
        variants: list[dict[str, object]] = [
            {"criteria_version": "v2"},
            {"criteria_version": None},
            {"judge_model": "judge-y"},
            {"judge_model": None},
            {"deepeval_version": "2.0.0"},
            {"deepeval_version": None},
            {"judge_specs": self._specs(threshold=0.9)},
            {"judge_specs": [MetricSpec(id="agent.task_completion", threshold=0.7)]},
        ]
        for override in variants:
            assert self._digest(**override) != base, override

    def test_metric_declaration_order_does_not_matter(self) -> None:
        assert self._digest() == self._digest(judge_specs=list(reversed(self._specs())))

    def test_empty_judge_specs_is_none_not_empty_string(self) -> None:
        # "没有生效的 judge 口径"如实缺席：None 与空串是两种事实
        assert (
            compute_judge_config_digest(
                criteria_version="v1",
                judge_model="judge-x",
                deepeval_version="1.0.0",
                judge_specs=[],
            )
            is None
        )


class TestEvalPolicyDigest:
    """policy_digest 只覆盖判分面（Spec §26.9 待回写）：判分面变 → 变；非判分面变 → 不变。"""

    @staticmethod
    def _policy(**overrides: object) -> EvalPolicy:
        fields: dict[str, object] = {
            "id": "qa",
            "display_name": "QA judge",
            "description": "生产问答判准",
            "judge_model": "judge-x",
            "timeout_seconds": 120.0,
            "metrics": [
                EvalPolicyMetric(id="m1", threshold=0.7),
                EvalPolicyMetric(id="m2", threshold=0.8),
            ],
        }
        fields.update(overrides)
        return EvalPolicy.model_validate(fields)

    def test_deterministic_and_shape(self) -> None:
        assert policy_digest(self._policy()) == policy_digest(self._policy())
        assert len(policy_digest(self._policy())) == 16

    def test_metric_declaration_order_does_not_matter(self) -> None:
        reordered = self._policy(
            metrics=[
                EvalPolicyMetric(id="m2", threshold=0.8),
                EvalPolicyMetric(id="m1", threshold=0.7),
            ]
        )
        assert policy_digest(self._policy()) == policy_digest(reordered)

    def test_judging_surface_changes_digest(self) -> None:
        base = policy_digest(self._policy())
        assert policy_digest(self._policy(judge_model="judge-y")) != base
        assert policy_digest(self._policy(judge_model=None)) != base
        # 阈值漂移 → 变
        assert (
            policy_digest(
                self._policy(
                    metrics=[
                        EvalPolicyMetric(id="m1", threshold=0.9),
                        EvalPolicyMetric(id="m2", threshold=0.8),
                    ]
                )
            )
            != base
        )
        # metric 集变化 → 变
        assert (
            policy_digest(self._policy(metrics=[EvalPolicyMetric(id="m1", threshold=0.7)])) != base
        )

    def test_non_judging_surface_keeps_digest(self) -> None:
        # "只覆盖判分面"的直接断言：id/display_name/description/timeout 变 → digest 不变
        base = policy_digest(self._policy())
        assert policy_digest(self._policy(id="qa-v2")) == base
        assert policy_digest(self._policy(display_name="QA judge v2")) == base
        assert policy_digest(self._policy(description="改了描述")) == base
        assert policy_digest(self._policy(timeout_seconds=30.0)) == base

    def test_scalar_entrypoint_agrees_with_policy_entrypoint(self) -> None:
        assert compute_eval_policy_digest(
            judge_model="judge-x",
            policy_metrics=[("m1", 0.7), ("m2", 0.8)],
        ) == policy_digest(self._policy())


class TestOnlineEvalSummaryDigest:
    """policy_digest 在执行口单点落账（Spec §26.9 待回写）：调用方免费获得。"""

    @staticmethod
    async def _run(policy: EvalPolicy) -> dict:
        class StubAdapter:
            def version(self) -> str | None:
                return "stub-1.0"

            async def evaluate(self, metric_id, threshold, trace, model=None):
                return 0.9, "stub"

        return await run_online_evaluation(
            pairs=[EvalPair(case_id="c1", input="q", actual_output="a")],
            policy=policy,
            adapter=StubAdapter(),
            unextractable_cases=[],
        )

    async def test_summary_carries_policy_digest(self) -> None:
        policy = EvalPolicy(
            id="qa",
            display_name="QA",
            judge_model="judge-x",
            metrics=[EvalPolicyMetric(id="m1", threshold=0.5)],
        )
        result = await self._run(policy)
        assert result["summary"]["policy_digest"] == policy_digest(policy)

    async def test_same_judging_surface_same_digest_regardless_of_metadata(self) -> None:
        first = EvalPolicy(
            id="qa",
            display_name="QA",
            judge_model="j",
            metrics=[EvalPolicyMetric(id="m1", threshold=0.5)],
        )
        renamed = EvalPolicy(
            id="qa-prod",
            display_name="QA prod",
            description="d",
            timeout_seconds=5.0,
            judge_model="j",
            metrics=[EvalPolicyMetric(id="m1", threshold=0.5)],
        )
        result_a = await self._run(first)
        result_b = await self._run(renamed)
        assert result_a["summary"]["policy_digest"] == result_b["summary"]["policy_digest"]


class TestAlignPolicyRows:
    """对齐判定语义（Spec §26.9 待回写）：闭式分布 + 剔除分列。"""

    @staticmethod
    def _row(case_id: str, metric: str, verdict: str, score: float | None) -> dict:
        return {"case_id": case_id, "metric": metric, "verdict": verdict, "score": score}

    def test_closed_form_distribution_and_delta(self) -> None:
        rows_a = [
            self._row("c1", "m", "pass", 0.8),
            self._row("c2", "m", "fail", 0.3),
            self._row("c3", "m", "pass", 0.9),
        ]
        rows_b = [
            self._row("c1", "m", "fail", 0.4),  # pass→fail = regression
            self._row("c2", "m", "pass", 0.9),  # fail→pass = improvement
            self._row("c3", "m", "pass", 0.9),  # pass→pass = same
        ]
        result = align_policy_rows(rows_a, rows_b)
        assert result["overall"] == {"n": 3, "improvement": 1, "regression": 1, "same": 1}
        assert result["per_metric"]["m"] == result["overall"]
        expected_delta = round(((-0.4) + 0.6 + 0.0) / 3, 4)
        assert result["mean_score_delta"]["m"] == expected_delta
        assert result["mean_score_delta_overall"] == expected_delta
        assert result["excluded"] == {}

    def test_fail_to_fail_is_same_not_regression(self) -> None:
        result = align_policy_rows(
            [self._row("c1", "m", "fail", 0.2)], [self._row("c1", "m", "fail", 0.3)]
        )
        assert result["overall"] == {"n": 1, "improvement": 0, "regression": 0, "same": 1}

    def test_single_side_skipped_excluded_and_itemized(self) -> None:
        result = align_policy_rows(
            [self._row("c1", "m", "pass", 0.8), self._row("c2", "m", "pass", 0.8)],
            [self._row("c1", "m", "skipped", None), self._row("c2", "m", "fail", 0.2)],
        )
        # 单侧 skipped 的对剔除并分列；另一对正常进判定
        assert result["excluded"] == {"skipped": 1}
        assert result["overall"] == {"n": 1, "improvement": 0, "regression": 1, "same": 0}

    def test_error_side_excluded_and_takes_priority_over_skipped(self) -> None:
        result = align_policy_rows(
            [self._row("c1", "m", "error", None), self._row("c2", "m", "error", None)],
            [self._row("c1", "m", "pass", 0.9), self._row("c2", "m", "skipped", None)],
        )
        assert result["excluded"] == {"error": 2}
        assert result["overall"]["n"] == 0

    def test_unmatched_metric_counted_not_paired(self) -> None:
        result = align_policy_rows(
            [self._row("c1", "m1", "pass", 0.9)],
            [self._row("c1", "m2", "pass", 0.9)],
        )
        # 两侧各自的孤儿行各计 1 次——不构造伪配对，也不静默丢弃
        assert result["excluded"] == {"metric_unmatched": 2}
        assert result["overall"]["n"] == 0

    def test_mean_score_delta_none_when_scores_missing(self) -> None:
        result = align_policy_rows(
            [self._row("c1", "m", "pass", None)], [self._row("c1", "m", "pass", None)]
        )
        assert result["overall"] == {"n": 1, "improvement": 0, "regression": 0, "same": 1}
        assert result["mean_score_delta"]["m"] is None
        assert result["mean_score_delta_overall"] is None


class TestAgreement:
    def test_confusion_precision_recall_kappa_closed_form(self) -> None:
        # tp=3 fp=2 tn=4 fn=1 → acc=0.7, P=0.6, R=0.75, pe=0.5 → κ=0.4
        items = (
            [_item(f"tp{i}", "m", judge="fail", human="fail") for i in range(3)]
            + [_item(f"fp{i}", "m", judge="fail", human="pass") for i in range(2)]
            + [_item(f"tn{i}", "m", judge="pass", human="pass") for i in range(4)]
            + [_item("fn1", "m", judge="pass", human="fail")]
        )
        agreement = compute_metric_agreement("m", items)
        assert agreement.n == 10
        assert agreement.accuracy == 0.7
        assert agreement.precision == 0.6
        assert agreement.recall == 0.75
        assert agreement.kappa == 0.4
        assert (agreement.tp, agreement.fp, agreement.tn, agreement.fn) == (3, 2, 4, 1)
        assert agreement.accuracy_ci95 is not None

    def test_degenerate_marginals_give_kappa_none(self) -> None:
        items = [_item(f"i{i}", "m", judge="fail", human="fail") for i in range(5)]
        agreement = compute_metric_agreement("m", items)
        assert agreement.kappa is None
        assert agreement.accuracy == 1.0

    def test_unlabeled_items_do_not_enter_denominator(self) -> None:
        items = [
            _item("a", "m", judge="fail", human="fail"),
            _item("b", "m", judge="pass", human=None),
        ]
        agreement = compute_metric_agreement("m", items)
        assert agreement.n == 1

    def test_near_threshold_cut_reports_separately(self) -> None:
        items = [
            _item("near-agree", "m", judge="fail", human="fail", score=0.72, threshold=0.7),
            _item("far-agree", "m", judge="pass", human="pass", score=0.95, threshold=0.7),
        ]
        agreement = compute_metric_agreement("m", items)
        assert agreement.near_threshold_accuracy == 1.0

    def test_mixed_thresholds_recorded_as_none(self) -> None:
        items = [
            _item("a", "m", judge="fail", human="fail", threshold=0.7),
            _item("b", "m", judge="fail", human="fail", threshold=0.8),
        ]
        assert compute_metric_agreement("m", items).threshold is None

    def test_human_kappa_perfect_agreement_is_one(self) -> None:
        # 双标完全一致且两侧边际非退化（1 fail + 2 pass）→ κ = 1.0
        items = [
            _item("a", "m", judge="fail", human="fail", human2="fail"),
            _item("b", "m", judge="pass", human="pass", human2="pass"),
            _item("c", "m", judge="pass", human="pass", human2="pass"),
        ]
        agreement = compute_metric_agreement("m", items)
        assert agreement.human_kappa == 1.0
        assert agreement.human_dual_n == 3

    def test_human_kappa_closed_form_mixed(self) -> None:
        # r1 vs r2：双 fail=2、r1 fail/r2 pass=1、双 pass=3、r1 pass/r2 fail=2
        # po=(2+3)/8=0.625；p1=3/8、p2=4/8 → pe=0.5 → κ=(0.625−0.5)/0.5=0.25
        items = (
            [_item(f"bf{i}", "m", judge="fail", human="fail", human2="fail") for i in range(2)]
            + [_item("fp", "m", judge="fail", human="fail", human2="pass")]
            + [_item(f"bp{i}", "m", judge="pass", human="pass", human2="pass") for i in range(3)]
            + [_item(f"pf{i}", "m", judge="pass", human="pass", human2="fail") for i in range(2)]
        )
        agreement = compute_metric_agreement("m", items)
        assert agreement.human_dual_n == 8
        assert agreement.human_kappa == 0.25

    def test_human_kappa_degenerate_marginals_is_none(self) -> None:
        items = [_item(f"i{i}", "m", judge="fail", human="fail", human2="fail") for i in range(3)]
        agreement = compute_metric_agreement("m", items)
        assert agreement.human_kappa is None
        assert agreement.human_dual_n == 3

    def test_single_rater_items_do_not_enter_ceiling(self) -> None:
        items = [
            _item("solo", "m", judge="fail", human="fail"),
            _item("dual", "m", judge="pass", human="pass", human2="pass"),
        ]
        agreement = compute_metric_agreement("m", items)
        assert agreement.human_dual_n == 1
        # 唯一双标项两侧全 pass（边际退化）→ κ 如实为 None，不是 0
        assert agreement.human_kappa is None

    def test_disagreement_total_and_cause_distribution(self) -> None:
        items = [
            _item("d1", "m", judge="fail", human="pass", cause="input_missing"),
            _item("d2", "m", judge="pass", human="fail", cause="rubric_ambiguous"),
            _item("d3", "m", judge="fail", human="pass"),  # 分歧但未归因
            _item("d4", "m", judge="fail", human="pass", cause="model_misunderstanding"),
            _item("a1", "m", judge="pass", human="pass"),
        ]
        agreement = compute_metric_agreement("m", items)
        assert agreement.disagreement_total == 4
        assert agreement.disagreement_causes == {
            "input_missing": 1,
            "rubric_ambiguous": 1,
            "model_misunderstanding": 1,
        }
        # 未归因数 = total − sum(causes)，不单独造字段
        assert agreement.disagreement_total - sum(agreement.disagreement_causes.values()) == 1
        # 归因不进混淆矩阵：n 与 accuracy 仍按 5 条 labeled 算（tp=0 fp=3 tn=1 fn=1）
        assert agreement.n == 5
        assert agreement.accuracy == 0.2


class TestVocabularyClosure:
    """封闭词汇的机械护栏（Spec §26.2）：常量（CLI/import 校验面）与
    Literal（模型校验面）是同一词汇的两处落点，分叉会让 CLI 写进模型
    拒收的 set 文件——两侧必须同步漂移。"""

    @staticmethod
    def _literal_values(field: str) -> set[str]:
        inner, _none = get_args(CalibrationItem.model_fields[field].annotation)
        return set(get_args(inner))

    def test_human_verdicts_match_literal(self) -> None:
        assert self._literal_values("human_verdict") == set(HUMAN_VERDICTS)
        assert self._literal_values("human_verdict_2") == set(HUMAN_VERDICTS)

    def test_disagreement_causes_match_literal(self) -> None:
        assert self._literal_values("disagreement_cause") == set(DISAGREEMENT_CAUSES)


class TestCalibrationStore:
    def test_roundtrip(self, tmp_path: Path) -> None:
        store = CalibrationStore(tmp_path)
        cal_set = CalibrationSet(
            cal_id="calset_abc",
            source_kind="run",
            source_ids=["r1"],
            seed=1,
            requested_n=5,
            items=[_item("i1", "m")],
        )
        store.save_set(cal_set)
        assert store.load_set("calset_abc").items[0].item_id == "i1"

    def test_missing_set_is_invalid_call(self, tmp_path: Path) -> None:
        try:
            CalibrationStore(tmp_path).load_set("calset_nope")
        except InvalidCallError:
            return
        raise AssertionError("missing set must raise InvalidCallError")

    def test_id_is_path_validated(self, tmp_path: Path) -> None:
        try:
            CalibrationStore(tmp_path).load_set("../escape")
        except InvalidCallError:
            return
        raise AssertionError("path traversal id must raise InvalidCallError")

    def test_list_sets_skips_corrupt_files(self, tmp_path: Path) -> None:
        store = CalibrationStore(tmp_path)
        store.save_set(
            CalibrationSet(
                cal_id="calset_ok", source_kind="run", source_ids=["r1"], seed=1, requested_n=1
            )
        )
        (store.root / "calset_bad.json").write_text("{not json", encoding="utf-8")
        assert [s.cal_id for s in store.list_sets()] == ["calset_ok"]

    def test_legacy_set_json_without_new_fields_loads(self, tmp_path: Path) -> None:
        """旧 set JSON（无版本/指纹/双标/归因字段）照常加载——pydantic 缺省兜底。"""
        store = CalibrationStore(tmp_path)
        store.root.mkdir(parents=True, exist_ok=True)
        legacy = {
            "cal_id": "calset_legacy",
            "created_at": "2026-01-01T00:00:00+00:00",
            "source_kind": "run",
            "source_ids": ["r1"],
            "seed": 1,
            "requested_n": 1,
            "items": [
                {
                    "item_id": "cal_x",
                    "source": "run",
                    "source_id": "r1",
                    "case_id": "c1",
                    "metric": "agent.task_completion",
                    "score": 0.9,
                    "threshold": 0.7,
                    "judge_verdict": "pass",
                }
            ],
        }
        (store.root / "calset_legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
        loaded = store.load_set("calset_legacy")
        assert loaded.criteria_version is None
        assert loaded.judge_config_digest is None
        assert loaded.items[0].human_verdict_2 is None
        assert loaded.items[0].human_rater == ""
        assert loaded.items[0].disagreement_cause is None


class TestCalibrateCli:
    def test_full_chain_create_import_evaluate(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        result = runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "6",
                "--seed",
                "3",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        assert len(cal_set.items) == 6
        assert cal_set.judge_model == "judge-x"

        # 全部按 judge 原判标注，再双向各翻 1 条（fail→pass 产生 fp，pass→fail 产生 fn）：
        # 3 fail + 3 pass → precision = recall = 2/3，accuracy = 4/6
        labels = [{"item": i.item_id, "verdict": i.judge_verdict} for i in cal_set.items]
        flip_to_pass = next(i for i in cal_set.items if i.judge_verdict == "fail")
        next(entry for entry in labels if entry["item"] == flip_to_pass.item_id)["verdict"] = "pass"
        flip_to_fail = next(i for i in cal_set.items if i.judge_verdict == "pass")
        next(entry for entry in labels if entry["item"] == flip_to_fail.item_id)["verdict"] = "fail"
        labels_file = tmp_path / "labels.yaml"
        labels_file.write_text(yaml.safe_dump({"labels": labels}), encoding="utf-8")

        imported = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert imported.exit_code == 0, imported.output

        evaluated = runner.invoke(
            app, ["calibrate", "evaluate", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert evaluated.exit_code == 0, evaluated.output
        # rich 会在 80 列内折行，断言只看单词
        assert "positive" in evaluated.output
        stored = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        assert len(stored.snapshots) == 1
        snapshot = stored.snapshots[0]
        assert snapshot.labeled == 6
        assert snapshot.overall.n == 6
        # 6 条里 3 条 judge=fail，翻走 1 条 → precision = recall = 2/3
        assert snapshot.overall.precision == 0.666667
        assert snapshot.overall.recall == 0.666667

        shown = runner.invoke(
            app, ["calibrate", "show", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert shown.exit_code == 0

    def test_import_labels_missing_file_is_invalid_not_traceback(self, tmp_path: Path) -> None:
        """C01 回归：labels 文件不存在 → exit 3，而不是裸 FileNotFoundError。"""
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        result = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(tmp_path / "nope.yaml"),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 3
        assert "无法读取" in result.output

    def test_import_labels_malformed_yaml_is_invalid(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        labels_file = tmp_path / "broken.yaml"
        labels_file.write_text("{unclosed: [", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 3
        assert "YAML" in result.output

    def test_import_rejects_unknown_item_atomically(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "4",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        labels_file = tmp_path / "labels.yaml"
        labels_file.write_text(
            yaml.safe_dump({"labels": [{"item": "cal_nope", "verdict": "pass"}]}), encoding="utf-8"
        )
        result = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 3
        # 全有或全无：失败的导入不得落任何标注
        stored = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        assert all(i.human_verdict is None for i in stored.items)

    def test_evaluate_without_labels_is_invalid(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        result = runner.invoke(
            app, ["calibrate", "evaluate", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert result.exit_code == 3

    def test_create_from_production_evaluations(self, tmp_path: Path) -> None:
        ProductionStore(tmp_path).save_evaluation(
            "tr_cal",
            {
                "evaluation_id": "eval_1",
                "rows": [
                    {
                        "case_id": "c1",
                        "metric": "agent.task_completion",
                        "threshold": 0.7,
                        "score": 0.9,
                        "verdict": "pass",
                    },
                    {
                        "case_id": "c2",
                        "metric": "agent.tool_correctness",
                        "threshold": 0.8,
                        "score": 0.4,
                        "verdict": "fail",
                    },
                ],
            },
        )
        result = runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "production:tr_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        assert cal_set.source_kind == "production"
        assert len(cal_set.items) == 2
        # 生产评测记录不含判准版本与指纹——与 judge_model=None 同一先例：不猜
        assert cal_set.criteria_version is None
        assert cal_set.judge_config_digest is None
        # 旧记录缺 policy_digest → None + missing 计数（Spec §26.9 待回写）
        assert cal_set.excluded.get("policy_digest_missing") == 1

    @staticmethod
    def _save_prod_evaluation(
        tmp_path: Path, trace_id: str, evaluation_id: str, digest: str | None
    ) -> None:
        row = {
            "case_id": f"c-{evaluation_id}",
            "metric": "agent.task_completion",
            "threshold": 0.7,
            "score": 0.9,
            "verdict": "pass",
        }
        summary: dict = {"policy": "qa"}
        if digest is not None:
            summary["policy_digest"] = digest
        ProductionStore(tmp_path).save_evaluation(
            trace_id, {"evaluation_id": evaluation_id, "rows": [row], "summary": summary}
        )

    def _create_production(self, tmp_path: Path, trace_id: str):
        return runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                f"production:{trace_id}",
                "--n",
                "4",
                "--data-dir",
                str(tmp_path),
            ],
        )

    def test_create_from_production_homogeneous_digest_is_recorded(self, tmp_path: Path) -> None:
        for eval_id in ("eval_1", "eval_2"):
            self._save_prod_evaluation(tmp_path, "tr_hom", eval_id, "aaaa1111bbbb2222")
        result = self._create_production(tmp_path, "tr_hom")
        assert result.exit_code == 0, result.output
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        # 同构 digest 落账（Spec §26.9 待回写）
        assert cal_set.judge_config_digest == "aaaa1111bbbb2222"
        assert not any(k.startswith("policy_digest") for k in cal_set.excluded)

    def test_create_from_production_mixed_digest_is_none_and_counted(self, tmp_path: Path) -> None:
        self._save_prod_evaluation(tmp_path, "tr_mix", "eval_1", "aaaa1111bbbb2222")
        self._save_prod_evaluation(tmp_path, "tr_mix", "eval_2", "cccc3333dddd4444")
        result = self._create_production(tmp_path, "tr_mix")
        assert result.exit_code == 0, result.output
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        # 异构 → None + 全部携带 digest 的份数进 excluded（无任何一份可代表集合）
        assert cal_set.judge_config_digest is None
        assert cal_set.excluded.get("policy_digest_mixed") == 2

    def test_create_from_production_missing_and_mixed_both_counted(self, tmp_path: Path) -> None:
        self._save_prod_evaluation(tmp_path, "tr_both", "eval_1", "aaaa1111bbbb2222")
        self._save_prod_evaluation(tmp_path, "tr_both", "eval_2", "cccc3333dddd4444")
        self._save_prod_evaluation(tmp_path, "tr_both", "eval_3", None)
        result = self._create_production(tmp_path, "tr_both")
        assert result.exit_code == 0, result.output
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        assert cal_set.judge_config_digest is None
        assert cal_set.excluded.get("policy_digest_missing") == 1
        assert cal_set.excluded.get("policy_digest_mixed") == 2

    def test_create_rejects_bad_source(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["calibrate", "create", "--source", "wat:xyz", "--data-dir", str(tmp_path)]
        )
        assert result.exit_code == 3

    def test_plan_prints_required_sample_size(self) -> None:
        result = runner.invoke(app, ["calibrate", "plan", "--precision", "0.05"])
        assert result.exit_code == 0
        assert "385" in result.output

    def test_list_reports_progress(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        result = runner.invoke(app, ["calibrate", "list", "--data-dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "0/2" in result.output

    def test_label_updates_progress(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        result = runner.invoke(
            app,
            [
                "calibrate",
                "label",
                cal_set.cal_id,
                "--item",
                cal_set.items[0].item_id,
                "--verdict",
                "pass",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0
        assert "labeled 1/2" in result.output

    def test_create_from_run_carries_version_fields_into_set_and_snapshot(
        self, tmp_path: Path
    ) -> None:
        _seed_run(
            tmp_path,
            "run_cal",
            criteria_version="rubric-v3",
            judge_config_digest="aaaa1111bbbb2222",
        )
        created = runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "4",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert created.exit_code == 0, created.output
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        assert cal_set.criteria_version == "rubric-v3"
        assert cal_set.judge_config_digest == "aaaa1111bbbb2222"

        labels = [{"item": i.item_id, "verdict": i.judge_verdict} for i in cal_set.items]
        labels_file = tmp_path / "labels.yaml"
        labels_file.write_text(yaml.safe_dump({"labels": labels}), encoding="utf-8")
        imported = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert imported.exit_code == 0, imported.output

        evaluated = runner.invoke(
            app, ["calibrate", "evaluate", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert evaluated.exit_code == 0, evaluated.output
        snapshot = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id).snapshots[-1]
        assert snapshot.criteria_version == "rubric-v3"
        assert snapshot.judge_config_digest == "aaaa1111bbbb2222"
        # evaluate / show / list 输出展示版本字段（digest 短形式）
        assert "rubric-v3" in evaluated.output
        assert "aaaa1111bbbb2222" in evaluated.output
        shown = runner.invoke(
            app, ["calibrate", "show", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert shown.exit_code == 0
        assert "rubric-v3" in shown.output

    def test_label_rounds_do_not_overwrite_each_other(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        target = cal_set.items[0]
        base = [
            "calibrate",
            "label",
            cal_set.cal_id,
            "--item",
            target.item_id,
            "--data-dir",
            str(tmp_path),
        ]
        first = runner.invoke(app, [*base, "--verdict", "fail", "--rater", "alice"])
        assert first.exit_code == 0, first.output
        second = runner.invoke(app, [*base, "--verdict", "pass", "--round", "2", "--rater", "bob"])
        assert second.exit_code == 0, second.output
        stored = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        item = next(i for i in stored.items if i.item_id == target.item_id)
        assert item.human_verdict == "fail"
        assert item.human_rater == "alice"
        assert item.human_verdict_2 == "pass"
        assert item.human_rater_2 == "bob"

        evaluated = runner.invoke(
            app, ["calibrate", "evaluate", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert evaluated.exit_code == 0, evaluated.output
        snapshot = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id).snapshots[-1]
        assert snapshot.overall.human_dual_n == 1
        # 有双标数据才渲染天花板解读行；无双标时整行缺席（不渲染空表）
        assert "ceiling" in evaluated.output

    def test_evaluate_without_dual_labels_has_no_ceiling_line(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        labels = [{"item": i.item_id, "verdict": i.judge_verdict} for i in cal_set.items]
        labels_file = tmp_path / "labels.yaml"
        labels_file.write_text(yaml.safe_dump({"labels": labels}), encoding="utf-8")
        runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        evaluated = runner.invoke(
            app, ["calibrate", "evaluate", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert evaluated.exit_code == 0, evaluated.output
        assert "ceiling" not in evaluated.output
        # 全按 judge 原判标注 → 无分歧，归因分布行同样缺席
        assert "disagreement" not in evaluated.output

    def test_cause_requires_labeled_and_disagreeing_item(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "2",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        pass_item = next(i for i in cal_set.items if i.judge_verdict == "pass")
        fail_item = next(i for i in cal_set.items if i.judge_verdict == "fail")
        base = [
            "calibrate",
            "label",
            cal_set.cal_id,
            "--data-dir",
            str(tmp_path),
        ]
        # 生效后仍未标注 → 拒绝：round 2 的 cause 依赖 round 1 human_verdict，
        # round 1 还没标时分歧不存在（round 1 判定在 Cause 校验里按生效值参与）
        unlabeled = runner.invoke(
            app,
            [
                *base,
                "--item",
                fail_item.item_id,
                "--verdict",
                "pass",
                "--round",
                "2",
                "--cause",
                "input_missing",
            ],
        )
        assert unlabeled.exit_code == 3
        assert "尚未标注" in unlabeled.output
        # 生效判定与 judge 一致 → 拒绝（一致项无归因对象）
        agree = runner.invoke(
            app, [*base, "--item", pass_item.item_id, "--verdict", "pass", "--rater", "alice"]
        )
        assert agree.exit_code == 0, agree.output
        cause_on_agree = runner.invoke(
            app,
            [
                *base,
                "--item",
                pass_item.item_id,
                "--verdict",
                "pass",
                "--cause",
                "rubric_ambiguous",
            ],
        )
        assert cause_on_agree.exit_code == 3
        assert "一致" in cause_on_agree.output
        # 非法 cause 词汇 → exit 3
        bad_word = runner.invoke(
            app, [*base, "--item", pass_item.item_id, "--verdict", "pass", "--cause", "whatever"]
        )
        assert bad_word.exit_code == 3
        # 分歧项一次调用内「verdict + cause」成功：生效判定 pass ≠ judge fail
        disagree = runner.invoke(
            app,
            [
                *base,
                "--item",
                fail_item.item_id,
                "--verdict",
                "pass",
                "--rater",
                "carol",
                "--cause",
                "input_missing",
            ],
        )
        assert disagree.exit_code == 0, disagree.output
        stored = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        target = next(i for i in stored.items if i.item_id == fail_item.item_id)
        assert target.disagreement_cause == "input_missing"
        assert target.human_verdict == "pass"
        assert target.human_rater == "carol"

        evaluated = runner.invoke(
            app, ["calibrate", "evaluate", cal_set.cal_id, "--data-dir", str(tmp_path)]
        )
        assert evaluated.exit_code == 0, evaluated.output
        assert "disagreement" in evaluated.output
        snapshot = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id).snapshots[-1]
        assert snapshot.overall.disagreement_total == 1
        # cause_on_agree 被拒 → pass_item 无归因；只有 fail_item 归因
        assert snapshot.overall.disagreement_causes == {"input_missing": 1}

    def test_import_labels_round2_rater_cause_and_atomicity(self, tmp_path: Path) -> None:
        _seed_run(tmp_path, "run_cal")
        runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cal",
                "--n",
                "4",
                "--data-dir",
                str(tmp_path),
            ],
        )
        cal_set = CalibrationStore(tmp_path / "state").list_sets()[0]
        pass_item = next(i for i in cal_set.items if i.judge_verdict == "pass")
        fail_item = next(i for i in cal_set.items if i.judge_verdict == "fail")
        another = next(
            i for i in cal_set.items if i.item_id not in {pass_item.item_id, fail_item.item_id}
        )

        good = {
            "labels": [
                {"item": pass_item.item_id, "verdict": "pass", "round": 2, "rater": "bob"},
                {"item": fail_item.item_id, "verdict": "pass", "cause": "input_missing"},
            ]
        }
        labels_file = tmp_path / "labels.yaml"
        labels_file.write_text(yaml.safe_dump(good), encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        stored = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        by_id = {i.item_id: i for i in stored.items}
        assert by_id[pass_item.item_id].human_verdict_2 == "pass"
        assert by_id[pass_item.item_id].human_rater_2 == "bob"
        assert by_id[pass_item.item_id].human_verdict is None  # round 2 不碰 round 1 字段
        assert by_id[fail_item.item_id].human_verdict == "pass"
        assert by_id[fail_item.item_id].disagreement_cause == "input_missing"

        # 非法 round：整批不落盘（全有或全无语义保持）
        bad = {
            "labels": [
                {"item": another.item_id, "verdict": "pass"},
                {"item": pass_item.item_id, "verdict": "pass", "round": 3},
            ]
        }
        labels_file.write_text(yaml.safe_dump(bad), encoding="utf-8")
        bad_result = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert bad_result.exit_code == 3
        stored2 = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        still = {i.item_id: i for i in stored2.items}
        assert still[another.item_id].human_verdict is None

        # 非法 cause：同样整批不落盘
        bad_cause_labels = {"labels": [{"item": another.item_id, "verdict": "fail", "cause": "x"}]}
        labels_file.write_text(yaml.safe_dump(bad_cause_labels), encoding="utf-8")
        bad_cause = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert bad_cause.exit_code == 3
        stored3 = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        assert next(i for i in stored3.items if i.item_id == another.item_id).human_verdict is None

        # cause 写在 round 1 未标注的 item 上（round 2 条目）：生效后仍未标注 →
        # 整批拒绝，round 2 既有标注不被覆盖
        labels_file.write_text(
            yaml.safe_dump(
                {
                    "labels": [
                        {
                            "item": pass_item.item_id,
                            "verdict": "fail",
                            "round": 2,
                            "cause": "input_missing",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        bad_cause_r2 = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert bad_cause_r2.exit_code == 3
        stored4 = CalibrationStore(tmp_path / "state").load_set(cal_set.cal_id)
        pass_after = next(i for i in stored4.items if i.item_id == pass_item.item_id)
        assert pass_after.human_verdict_2 == "pass"  # 好批次写入的 round 2 未被坏批次覆盖
        assert pass_after.disagreement_cause is None


class TestRunnerDigestBackfill:
    """A2：runner 在 judge_specs 解析（含 degradation）后回填 meta.judge_config_digest。"""

    @staticmethod
    async def _run(evals_tree, fixtures_root) -> tuple:
        evals_root, data_root = evals_tree
        cfg = RunConfig(
            evals_root=evals_root,
            fixtures_root=fixtures_root,
            data_root=data_root,
            benchmark="database-core",
            agent_endpoint="fake://",
            tag_filter=["smoke"],
            repeat=1,
        )
        runner = Runner(cfg)
        outcome = await runner.run()
        meta, _results = runner.store.load_run(outcome.run_id)
        return outcome, meta

    async def test_no_effective_judge_specs_yields_none(self, evals_tree, fixtures_root) -> None:
        # conftest 的 deepeval_absent 夹具 → probe 全降级 → 生效集为空 → None（不是空串）
        _outcome, meta = await self._run(evals_tree, fixtures_root)
        assert meta.judge_config_digest is None

    async def test_effective_specs_backfill_digest(
        self, evals_tree, fixtures_root, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_eval.evaluators import deepeval_adapter as adapter_mod

        async def fake_evaluate(self, metric_id, threshold, trace, *, model=None):
            return 0.9, "stub"

        monkeypatch.setattr(
            adapter_mod.DeepEvalCapabilityAdapter,
            "probe",
            lambda self: {"agent.task_completion": True, "agent.tool_correctness": True},
        )
        monkeypatch.setattr(
            adapter_mod.DeepEvalCapabilityAdapter, "version", lambda self: "9.9.9-test"
        )
        monkeypatch.setattr(adapter_mod.DeepEvalCapabilityAdapter, "evaluate", fake_evaluate)
        _outcome, meta = await self._run(evals_tree, fixtures_root)
        expected = compute_judge_config_digest(
            criteria_version=meta.criteria_version,
            judge_model=meta.judge_model,
            deepeval_version="9.9.9-test",
            judge_specs=[
                MetricSpec(id="agent.task_completion", threshold=0.7),
                MetricSpec(id="agent.tool_correctness", threshold=0.8),
            ],
        )
        assert meta.judge_config_digest is not None
        assert meta.judge_config_digest == expected


class TestPolicyCompareCli:
    """policy-compare 全链路（stub judge，Spec §26.9 待回写）。

    分数由 stub 按 (case input, judge_model) 控制——两份 policy 的判分面差异
    （judge_model / threshold）因此可控地映射为 improvement / regression / same。
    """

    JUDGE_MODELS = {"pol-a": "judge-a", "pol-b": "judge-b"}

    @staticmethod
    def _write_policies(tmp_path: Path) -> Path:
        evals_root = tmp_path / "evals"
        directory = evals_root / "eval-policies"
        directory.mkdir(parents=True)
        for policy_id, judge_model in TestPolicyCompareCli.JUDGE_MODELS.items():
            body = (
                f"id: {policy_id}\n"
                "display_name: 判准\n"
                f"judge_model: {judge_model}\n"
                "metrics:\n  - id: m\n    threshold: 0.5\n"
            )
            (directory / f"{policy_id}.yaml").write_text(body, encoding="utf-8")
        return evals_root

    @staticmethod
    def _ingest_trace(tmp_path: Path, case_ids: list[str]) -> str:
        cases = [
            {
                "case_id": case_id,
                "events": [
                    {
                        "event_id": f"{case_id}-1",
                        "trace_id": "t1",
                        "type": "run.started",
                        "timestamp": "2026-10-29T10:00:00+08:00",
                        "data": {"input": f"q-{case_id}"},
                    },
                    {
                        "event_id": f"{case_id}-2",
                        "trace_id": "t1",
                        "type": "run.finished",
                        "timestamp": "2026-10-29T10:00:01+08:00",
                        "data": {"output": f"out-{case_id}", "status": "success"},
                    },
                ],
            }
            for case_id in case_ids
        ]
        return ProductionStore(tmp_path).save(source="platform", cases=cases, trace_id="t1")

    @staticmethod
    def _stub_judge(
        monkeypatch: pytest.MonkeyPatch,
        scores: dict[tuple[str, str], float],
        error_inputs: frozenset[str] = frozenset(),
    ) -> None:
        from agent_eval.evaluators import deepeval_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod.DeepEvalCapabilityAdapter, "version", lambda self: "9.9.9-test"
        )

        async def fake_evaluate(self, metric_id, threshold, trace, *, model=None):
            if trace["input"] in error_inputs:
                raise RuntimeError("judge provider down")
            value = scores.get((trace["input"], model))
            if value is None:
                raise RuntimeError(f"no stub score for {trace['input']!r} / {model!r}")
            return value, "stub"

        monkeypatch.setattr(adapter_mod.DeepEvalCapabilityAdapter, "evaluate", fake_evaluate)

    def _invoke(self, tmp_path: Path, evals_root: Path, *, policy_a: str = "pol-a"):
        return runner.invoke(
            app,
            [
                "calibrate",
                "policy-compare",
                "--trace",
                "t1",
                "--policy-a",
                policy_a,
                "--policy-b",
                "pol-b",
                "--root",
                str(evals_root),
                "--data-dir",
                str(tmp_path),
            ],
        )

    def _expected_digest(self, policy_id: str) -> str:
        return compute_eval_policy_digest(
            judge_model=self.JUDGE_MODELS[policy_id],
            policy_metrics=[("m", 0.5)],
        )

    def test_full_link_distribution_delta_and_digests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 闭式场景：reg / imp / same 三对 + 一对基线侧 error 被剔除
        self._stub_judge(
            monkeypatch,
            {
                ("q-reg", "judge-a"): 0.8,  # pass→fail regression，Δ=-0.4
                ("q-reg", "judge-b"): 0.4,
                ("q-imp", "judge-a"): 0.3,  # fail→pass improvement，Δ=+0.6
                ("q-imp", "judge-b"): 0.9,
                ("q-same", "judge-a"): 0.9,  # pass→pass same，Δ=0
                ("q-same", "judge-b"): 0.9,
            },
            error_inputs=frozenset({"q-err"}),
        )
        evals_root = self._write_policies(tmp_path)
        self._ingest_trace(tmp_path, ["reg", "imp", "same", "err"])
        result = self._invoke(tmp_path, evals_root)
        assert result.exit_code == 0, result.output
        output = result.output
        # 头行绑定两侧配置版本（论点⑤：结果记录生效配置版本）
        assert self._expected_digest("pol-a") in output
        assert self._expected_digest("pol-b") in output
        assert "pol-a" in output and "pol-b" in output
        assert "n=3" in output
        # overall 均值分差 (−0.4 + 0.6 + 0.0) / 3
        assert "+0.0667" in output
        # 基线侧 error 的对剔除并分列
        assert "'error': 1" in output

    def test_unextractable_cases_counted_not_paired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_judge(monkeypatch, {("q-c1", "judge-a"): 0.9, ("q-c1", "judge-b"): 0.9})
        evals_root = self._write_policies(tmp_path)
        ProductionStore(tmp_path).save(
            source="platform",
            trace_id="t1",
            cases=[
                {
                    "case_id": "c1",
                    "events": [
                        {"event_id": "e1", "type": "run.started", "data": {"input": "q-c1"}},
                        {"event_id": "e2", "type": "run.finished", "data": {"output": "out"}},
                    ],
                },
                # 无 input/output：提取不出，如实计数不进比对
                {
                    "case_id": "c-empty",
                    "events": [{"event_id": "e3", "type": "tool.call", "data": {"name": "x"}}],
                },
            ],
        )
        result = self._invoke(tmp_path, evals_root)
        assert result.exit_code == 0, result.output
        assert "unextractable" in result.output

    def test_unknown_policy_is_invalid_call(self, tmp_path: Path) -> None:
        evals_root = self._write_policies(tmp_path)
        self._ingest_trace(tmp_path, ["c1"])
        result = self._invoke(tmp_path, evals_root, policy_a="ghost")
        assert result.exit_code == 3
        assert "ghost" in result.output

    def test_unknown_trace_is_invalid_call(self, tmp_path: Path) -> None:
        evals_root = self._write_policies(tmp_path)
        result = runner.invoke(
            app,
            [
                "calibrate",
                "policy-compare",
                "--trace",
                "no-such-trace",
                "--policy-a",
                "pol-a",
                "--policy-b",
                "pol-b",
                "--root",
                str(evals_root),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 3
        assert "no-such-trace" in result.output

    def test_all_skipped_is_invalid_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_eval.evaluators import deepeval_adapter as adapter_mod

        # DeepEval 缺失 → 双侧全 skipped → 无可比判定（exit 3）
        monkeypatch.setattr(adapter_mod.DeepEvalCapabilityAdapter, "version", lambda self: None)
        evals_root = self._write_policies(tmp_path)
        self._ingest_trace(tmp_path, ["c1", "c2"])
        result = self._invoke(tmp_path, evals_root)
        assert result.exit_code == 3
        assert "无可比判定" in result.output


class TestAttributionLifecycle:
    """归因与分歧同生共死（Spec §26.8）：改判致一致时旧 cause 随分歧消失。

    校验只挡"写 cause 时"的非法写入还不够——round 1 判定被改写成与 judge 一致后，
    落盘的旧 cause 会违背"归因只能写在分歧上"的不变量。统计端不受污染
    （agreement 只在当前分歧子集上计数），但字段本身不能说谎。
    """

    def _create_set(self, tmp_path: Path) -> CalibrationSet:
        _seed_run(tmp_path, "run_cause")
        result = runner.invoke(
            app,
            [
                "calibrate",
                "create",
                "--source",
                "run:run_cause",
                "--n",
                "4",
                "--seed",
                "7",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        return CalibrationStore(tmp_path / "state").list_sets()[0]

    def test_relabel_to_agreement_clears_stale_cause(self, tmp_path: Path) -> None:
        cal_set = self._create_set(tmp_path)
        store = CalibrationStore(tmp_path / "state")
        # judge=fail 的 item：人工说 pass 才是分歧；说 fail 是一致，cause 会被拒
        fail_item = next(i for i in cal_set.items if i.judge_verdict == "fail")
        labeled = runner.invoke(
            app,
            [
                "calibrate",
                "label",
                cal_set.cal_id,
                "--item",
                fail_item.item_id,
                "--verdict",
                "pass",
                "--cause",
                "input_missing",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert labeled.exit_code == 0, labeled.output
        stored = store.load_set(cal_set.cal_id)
        target = next(i for i in stored.items if i.item_id == fail_item.item_id)
        assert target.disagreement_cause == "input_missing"

        relabeled = runner.invoke(
            app,
            [
                "calibrate",
                "label",
                cal_set.cal_id,
                "--item",
                fail_item.item_id,
                "--verdict",
                "fail",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert relabeled.exit_code == 0, relabeled.output
        stored = store.load_set(cal_set.cal_id)
        target = next(i for i in stored.items if i.item_id == fail_item.item_id)
        assert target.disagreement_cause is None

    def test_relabel_still_disagree_keeps_cause(self, tmp_path: Path) -> None:
        cal_set = self._create_set(tmp_path)
        store = CalibrationStore(tmp_path / "state")
        fail_item = next(i for i in cal_set.items if i.judge_verdict == "fail")
        first = runner.invoke(
            app,
            [
                "calibrate",
                "label",
                cal_set.cal_id,
                "--item",
                fail_item.item_id,
                "--verdict",
                "pass",
                "--cause",
                "rubric_ambiguous",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert first.exit_code == 0, first.output
        again = runner.invoke(
            app,
            [
                "calibrate",
                "label",
                cal_set.cal_id,
                "--item",
                fail_item.item_id,
                "--verdict",
                "pass",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert again.exit_code == 0, again.output
        stored = store.load_set(cal_set.cal_id)
        target = next(i for i in stored.items if i.item_id == fail_item.item_id)
        assert target.disagreement_cause == "rubric_ambiguous"

    def test_import_relabel_to_agreement_clears_stale_cause(self, tmp_path: Path) -> None:
        cal_set = self._create_set(tmp_path)
        store = CalibrationStore(tmp_path / "state")
        fail_item = next(i for i in cal_set.items if i.judge_verdict == "fail")
        labels_file = tmp_path / "labels.yaml"
        labels_file.write_text(
            yaml.safe_dump(
                {
                    "labels": [
                        {
                            "item": fail_item.item_id,
                            "verdict": "pass",
                            "cause": "model_misunderstanding",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        imported = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert imported.exit_code == 0, imported.output
        labels_file.write_text(
            yaml.safe_dump({"labels": [{"item": fail_item.item_id, "verdict": "fail"}]}),
            encoding="utf-8",
        )
        imported = runner.invoke(
            app,
            [
                "calibrate",
                "import-labels",
                cal_set.cal_id,
                str(labels_file),
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert imported.exit_code == 0, imported.output
        stored = store.load_set(cal_set.cal_id)
        target = next(i for i in stored.items if i.item_id == fail_item.item_id)
        assert target.disagreement_cause is None
