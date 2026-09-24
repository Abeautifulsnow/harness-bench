"""Gate 规则集测试（PRD §64–§69/§108，Spec §6.1/§6.2）。

重点覆盖"门禁失真"这一类缺陷：门禁声明了却从不生效的约束不该静默放行。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_eval.loading.loader import load_benchmark, load_dataset, load_suites, resolve_suites
from agent_eval.models.results import CaseRunResult, CaseStatus
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.quality.gates import evaluate_gate, exit_code_for, load_gate_rules
from agent_eval.reports.aggregate import build_aggregate
from agent_eval.runner.runner import RunConfig, Runner

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "evals"


def _meta(run_id: str = "run1", **overrides) -> RunMetadata:
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


def _result(case_id: str = "c1", **overrides) -> CaseRunResult:
    payload = {
        "id": f"cr-{case_id}",
        "run_id": "run1",
        "case_id": case_id,
        "case_version": 1,
        "iteration": 1,
        "status": CaseStatus.PASS,
    }
    payload.update(overrides)
    return CaseRunResult(**payload)


def _rule(report, name: str):
    return next((r for r in report.rules if r.rule == name), None)


class TestSuiteCoverageRule:
    """PRD §108：Gate 声明的必跑套件必须真的执行过。"""

    def test_missing_suites_fail_the_rule(self) -> None:
        rules = load_gate_rules(EVALS, "release")
        assert rules.suites == ["golden", "regression", "security", "core"]
        meta = _meta(suites_covered={"smoke": 1})
        report = evaluate_gate(build_aggregate(meta, [_result()]), rules, None)

        rule = _rule(report, "suites.coverage")
        assert rule is not None
        assert rule.verdict == "fail"
        assert rule.blocking is True
        assert rule.affected_case_runs == ["golden", "regression", "security", "core"]
        assert "PRD §108" in rule.detail
        assert report.verdict == "fail"

    def test_all_suites_covered_passes(self) -> None:
        rules = load_gate_rules(EVALS, "release")
        meta = _meta(
            suites_covered={"golden": 3, "regression": 5, "security": 8, "core": 12},
        )
        report = evaluate_gate(build_aggregate(meta, [_result()]), rules, None)

        rule = _rule(report, "suites.coverage")
        assert rule is not None
        assert rule.verdict == "pass"
        assert rule.observed == 4.0 and rule.threshold == 4.0
        assert rule.affected_case_runs == []

    def test_suite_selected_zero_cases_is_not_a_pass(self) -> None:
        """套件跑了个空仍是未覆盖：这正是"没 case 就没失败"的平凡通过。"""
        rules = load_gate_rules(EVALS, "release")
        meta = _meta(suites_covered={"golden": 0, "regression": 1, "security": 1, "core": 1})
        report = evaluate_gate(build_aggregate(meta, [_result()]), rules, None)

        rule = _rule(report, "suites.coverage")
        assert rule is not None
        assert rule.verdict == "fail"
        assert rule.affected_case_runs == ["golden"]
        assert "0 个 case" in rule.detail

    def test_empty_suites_declaration_produces_no_rule(self) -> None:
        """``suites: []`` = 不约束，而不是"任何套件都不许跑"（PR/Main Gate 的现状）。"""
        for name in ("pr", "main"):
            rules = load_gate_rules(EVALS, name)
            assert rules.suites == []
            report = evaluate_gate(build_aggregate(_meta(), [_result()]), rules, None)
            assert _rule(report, "suites.coverage") is None

    def test_removing_suites_makes_the_rule_disappear_not_pass(self, tmp_path: Path) -> None:
        """反例：删掉 release.yaml 的 suites 后规则消失，而不是变成 PASS。"""
        import shutil

        root = tmp_path / "evals"
        shutil.copytree(EVALS, root)
        release = root / "gates" / "release.yaml"
        data = yaml.safe_load(release.read_text(encoding="utf-8"))
        data.pop("suites", None)
        release.write_text(yaml.safe_dump(data), encoding="utf-8")

        rules = load_gate_rules(root, "release")
        assert rules.suites == []
        report = evaluate_gate(build_aggregate(_meta(), [_result()]), rules, None)
        assert _rule(report, "suites.coverage") is None


class TestResolveSuites:
    def test_counts_are_reported_per_suite(self) -> None:
        benchmark = load_benchmark(EVALS, "database-core")
        suites = load_suites(EVALS)
        _, cases = load_dataset(EVALS, benchmark.dataset)
        selected, counts = resolve_suites(benchmark, suites, cases)

        assert set(counts) == {"smoke", "core"}
        assert sum(counts.values()) >= len(selected)  # 套件可重叠
        assert all(count > 0 for count in counts.values())

    def test_tag_filter_shrinks_counts_not_silently(self) -> None:
        """被 tag 过滤掉的套件计数为 0 → 未覆盖，而不是"跑过了"。"""
        benchmark = load_benchmark(EVALS, "database-core")
        suites = load_suites(EVALS)
        _, cases = load_dataset(EVALS, benchmark.dataset)
        _, counts = resolve_suites(benchmark, suites, cases, None, ["multi-turn"])
        assert counts["smoke"] == 1  # database.query.multi_turn_refine
        assert counts["core"] == 0

    def test_explicit_suite_filter_can_override_the_benchmark(self) -> None:
        benchmark = load_benchmark(EVALS, "database-core")
        suites = load_suites(EVALS)
        _, cases = load_dataset(EVALS, benchmark.dataset)
        _, counts = resolve_suites(benchmark, suites, cases, ["core"])
        assert set(counts) == {"core"}

    def test_unknown_suite_names_the_defined_ones(self) -> None:
        from agent_eval.errors import InvalidCallError

        benchmark = load_benchmark(EVALS, "database-core")
        suites = load_suites(EVALS)
        _, cases = load_dataset(EVALS, benchmark.dataset)
        with pytest.raises(InvalidCallError, match="defined suites"):
            resolve_suites(benchmark, suites, cases, ["ghost"])


class TestReleaseGateSuites:
    """PRD §108 验收：只跑 smoke 时，用 release 规则判 Gate 必须 FAIL。"""

    def test_release_suite_definitions_exist(self) -> None:
        suites = load_suites(EVALS)
        assert {"golden", "regression", "security"} <= set(suites)
        assert "security" in suites["security"].tags
        assert "red-team" in suites["security"].tags

    async def test_smoke_only_run_fails_the_release_gate(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
        cfg = RunConfig(
            evals_root=evals_root,
            fixtures_root=fx,
            data_root=data_root,
            benchmark="database-core",
            agent_endpoint="fake://",
            tag_filter=["smoke"],
            repeat=1,
        )
        outcome = await Runner(cfg).run()
        assert outcome.exit_code == 0  # PR Gate 下 smoke 全绿
        assert outcome.aggregate is not None
        assert set(outcome.aggregate.run.suites_covered) == {"smoke", "core"}

        # 重放为 Release 规则集：4 个必跑套件里 golden/regression/security 都没执行
        rules = load_gate_rules(evals_root, "release")
        report = evaluate_gate(outcome.aggregate, rules, outcome.aggregate.comparison)

        rule = _rule(report, "suites.coverage")
        assert rule is not None and rule.verdict == "fail"
        assert rule.affected_case_runs == ["golden", "regression", "security"]
        assert report.verdict == "fail"
        assert exit_code_for(report, outcome.aggregate) == 1

    async def test_gate_required_suites_are_actually_executed(
        self, evals_tree, fixtures_root
    ) -> None:
        """Gate 声明的必跑套件会扩宽实际选择面，而不只是事后判定。"""
        evals_root, data_root = evals_tree
        fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
        (evals_root / "gates" / "pr.yaml").write_text(
            yaml.safe_dump(
                {
                    "gate": "pr",
                    "suites": ["smoke", "golden"],
                    "rules": {"security": {"max_failures": 0}},
                }
            ),
            encoding="utf-8",
        )
        cfg = RunConfig(
            evals_root=evals_root,
            fixtures_root=fx,
            data_root=data_root,
            benchmark="database-core",
            agent_endpoint="fake://",
            repeat=1,
        )
        outcome = await Runner(cfg).run()
        assert outcome.aggregate is not None
        # golden 被 Gate 要求执行，所以出现在覆盖记录里……
        assert "golden" in outcome.aggregate.run.suites_covered
        # ……但仓库里还没有 golden case → 计数为 0 → 显式判"未覆盖"而非平凡通过
        assert outcome.aggregate.run.suites_covered["golden"] == 0
        assert outcome.exit_code == 1

    async def test_explicit_suite_flag_replaces_selection(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
        cfg = RunConfig(
            evals_root=evals_root,
            fixtures_root=fx,
            data_root=data_root,
            benchmark="database-core",
            agent_endpoint="fake://",
            suites=["smoke"],
            repeat=1,
        )
        outcome = await Runner(cfg).run()
        assert outcome.aggregate is not None
        assert set(outcome.aggregate.run.suites_covered) == {"smoke"}
        assert outcome.exit_code == 0
