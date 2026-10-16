"""Judge Calibration 的单元与 CLI 全链路测试（Spec §26）。

标注链路：create（抽样）→ import-labels / label → evaluate（快照）。
judge 隔离：校准只读已落盘的判定事实，不触发 judge，无需 deepeval。
"""

from __future__ import annotations

from pathlib import Path

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
from agent_eval.models.calibration import CalibrationItem, CalibrationSet
from agent_eval.models.results import CaseRunResult, MetricResultModel
from agent_eval.models.run import RunMetadata, RunStatus
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
    )


def _seed_run(data_dir: Path, run_id: str, *, items_per_case: int = 3) -> None:
    """落一个含 2 case × 3 iteration × 每轮 2 条 agent.* 判定的 run。"""
    store = RunStore(data_dir / "runs")
    store.create_run(_meta(run_id))
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
