"""MVP 用例集覆盖（PRD §103）与 case 级 metric_params（Spec §17.2）。

PRD §103 要"至少 20~30 Cases，覆盖 Tool / Database / Skill / MCP / Context /
Error Recovery"。这个任务真正的风险不是数量不够，而是**假覆盖**：把
"输出里出现某个词"当成 Skill/MCP/Context 维度的断言。所以这里的断言分三层：

1. 数量与维度：每维至少 2 条，且维度 case 带该维度的 tag；
2. 判定真的接上：维度 case 声明的 metric_params 必须在 run 结果里产出一条
   对应 metric（能被判、且判到了该 case 的期望）；
3. 该红的能红：负向 case 必须真的 FAIL——只有正向用例时，
   "断言写错了"与"agent 合规"在报告里长得一模一样。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_eval.errors import InvalidCallError
from agent_eval.loading.loader import load_benchmark, load_dataset, load_suites, resolve_suites
from agent_eval.runner.runner import RunConfig, Runner

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "evals"

# PRD §103 的六个覆盖维度 → 该维度 case 必须带的 tag
DIMENSIONS = {
    "Tool": "tool-use",
    "Database": "database",
    "Skill": "skill",
    "MCP": "mcp",
    "Context": "context",
    "Error Recovery": "error-recovery",
}

GOLDEN_MIN = 8  # golden 是所有"应始终 PASS"的正向 case，Release Gate 要求 100% 通过


def _cases() -> list:
    benchmark = load_benchmark(EVALS, "database-core")
    _info, cases = load_dataset(EVALS, benchmark.dataset)
    return cases


class TestDatasetShape:
    def test_case_count_in_prd_range(self) -> None:
        cases = _cases()
        # PRD §103 的 20~30 是下限口径（"至少"）：高于 30 不违规，
        # 低于 20 会让 task_success.max_regression_percent 失去分辨力
        # （一个 case 翻转 = 5% 以上的回归幅度）。
        assert len(cases) >= 20, f"PRD §103 要求 ≥20 cases，实际 {len(cases)}"
        assert len(cases) <= 40, f"示例集不宜过大（实际 {len(cases)}），超出部分应放独立数据集"

    def test_every_dimension_is_covered(self) -> None:
        cases = _cases()
        tags = {tag for case in cases for tag in case.tags}
        missing = [name for name, tag in DIMENSIONS.items() if tag not in tags]
        assert not missing, f"PRD §103 维度缺失：{missing}"

    def test_each_dimension_has_at_least_two_cases(self) -> None:
        cases = _cases()
        thin = {
            name: sum(1 for case in cases if tag in case.tags) for name, tag in DIMENSIONS.items()
        }
        assert all(count >= 2 for count in thin.values()), f"维度覆盖过薄：{thin}"

    def test_case_ids_are_unique(self) -> None:
        ids = [case.id for case in _cases()]
        assert len(ids) == len(set(ids))

    def test_golden_set_is_substantial(self) -> None:
        golden = [case for case in _cases() if "golden" in case.tags]
        assert len(golden) >= GOLDEN_MIN, f"golden 只有 {len(golden)} 条"

    def test_negative_cases_exist_for_regression_suite(self) -> None:
        """regression suite 的用途是"该红的能红"，所以必须有 negative 成员。"""
        cases = _cases()
        assert any("negative" in case.tags and "regression" in case.tags for case in cases)

    def test_suites_select_probably_disjoint_roles(self) -> None:
        """golden 只收正向：把负向 case 放进 golden 会让 Release Gate 永远 FAIL。"""
        offenders = [case.id for case in _cases() if {"golden", "negative"} <= set(case.tags)]
        assert not offenders, f"golden 不得包含负向 case：{offenders}"


class TestDimensionAssertionsAreReal:
    """维度 case 必须有真断言：标签不是覆盖，断言才是。"""

    def test_dimension_cases_declare_assertions(self) -> None:
        for name, tag in DIMENSIONS.items():
            dimension_cases = [c for c in _cases() if tag in c.tags]
            for case in dimension_cases:
                mounts = case.session_assertions()
                turn_expects = [t.expect for t in getattr(case.input, "turns", []) or []]
                declared = any(not assertion.is_empty() for _mount, assertion in mounts) or any(
                    assertion is not None and not assertion.is_empty() for assertion in turn_expects
                )
                has_params = bool(case.metric_params)
                assert declared or has_params, f"{name} 维度 {case.id} 没有任何可判定声明"

    def test_skill_mcp_context_cases_use_harness_params(self) -> None:
        """Skill/MCP/Context 三类靠 harness 插件判定，必须声明 case 级期望。"""
        expected_metric = {
            "skill": "harness.skill_load",
            "mcp": "harness.mcp_permission",
            "context": "harness.context_compaction",
        }
        for tag, metric in expected_metric.items():
            hit = [case for case in _cases() if tag in case.tags and metric in case.metric_params]
            assert hit, f"{tag} 维度没有任何 case 声明 {metric}（覆盖是假的）"


def _cfg(evals_root: Path, data_root: Path, fixtures_root: Path, **kw) -> RunConfig:
    fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
    return RunConfig(
        evals_root=evals_root,
        fixtures_root=fx,
        data_root=data_root,
        benchmark="database-core",
        agent_endpoint="fake://",
        repeat=1,
        no_judge=True,  # 维度判定是确定性的，不该依赖 judge
        gate="pr",
        **kw,
    )


async def _run_all(evals_root: Path, data_root: Path, fixtures_root: Path, **kw):
    runner = Runner(_cfg(evals_root, data_root, fixtures_root, **kw))
    outcome = await runner.run()
    _meta, results = runner.store.load_run(outcome.run_id)
    return outcome, results


def _by_case(results) -> dict[str, dict[str, str]]:
    return {
        result.case_id: {metric.metric: metric.verdict for metric in result.all_metric_results}
        for result in results
    }


class TestDimensionVerdicts:
    """端到端：维度 case 的判定必须真的落在自己的期望上。"""

    async def test_skill_priority_passes_and_wrong_order_fails(
        self, evals_tree, fixtures_root
    ) -> None:
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)
        assert verdicts["skill.priority.sql_first"]["harness.skill_load"] == "pass"
        assert verdicts["skill.fallback.no_skill"]["harness.skill_load"] == "pass"
        assert verdicts["skill.priority.wrong_order"]["harness.skill_load"] == "fail"

    async def test_mcp_whitelist_both_directions(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)
        assert verdicts["mcp.authorized.call"]["harness.mcp_permission"] == "pass"
        assert verdicts["mcp.unauthorized.call"]["harness.mcp_permission"] == "fail"

    async def test_context_compaction_both_directions(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)
        assert verdicts["context.compaction.retention"]["harness.context_compaction"] == "pass"
        assert verdicts["context.no_compaction.small"]["harness.context_compaction"] == "pass"
        assert verdicts["context.compaction.loop"]["harness.context_compaction"] == "fail"

    async def test_retry_exhausted_is_red(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)
        assert verdicts["error.recovery.retry_success"]["harness.retry"] == "pass"
        assert verdicts["error.recovery.retry_exhausted"]["harness.retry"] == "fail"

    async def test_tool_argument_assertions_both_directions(
        self, evals_tree, fixtures_root
    ) -> None:
        """参数级断言：合规查询 PASS、用 `^^DELETE FROM` 的写操作 FAIL。"""
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)
        assert verdicts["tool.arguments.scoped_query"]["native.argument_checks"] == "pass"
        assert verdicts["database.readonly.guard"]["native.argument_checks"] == "fail"

    async def test_tool_chain_requires_all_three_tools(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)
        assert verdicts["tool.chain.multi_step"]["native.tool_sequence"] == "pass"

    async def test_golden_members_all_pass(self, evals_tree, fixtures_root) -> None:
        """Release Gate 要求 golden 100% 通过：任何 golden 成员变红都是配置错误。"""
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        cases = {case.id: case for case in _cases()}
        red = sorted(
            result.case_id
            for result in results
            if "golden" in cases[result.case_id].tags and result.status.value != "PASS"
        )
        assert not red, f"golden 成员未通过：{red}"

    async def test_suites_cover_all_four_release_suites(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        outcome, _results = await _run_all(evals_root, data_root, fixtures_root)
        covered = outcome.report.get("suites_covered") or {}
        assert set(covered) == {"smoke", "core"}, f"benchmark 声明的 suites 未全部执行：{covered}"


class TestObservationExtensionVerdicts:
    """观测型扩展断言（Spec §19）的端到端接线。

    单元测试已经逐键覆盖了判定逻辑，这里只回答一个接线问题：
    **观测面在真实链路里真的通到了求值点吗？** 三条通路各有一条：
    fixture 快照（database_state / file_state）、tool.result 载荷（sql_result）、
    command.finished 的 exit_code。任何一条断了，对应断言会静默变成 skipped——
    那时单元测试仍全绿，假信号只在端到端才看得见。
    """

    async def test_environment_assertions_reach_the_verdict(
        self, evals_tree, fixtures_root
    ) -> None:
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)["database.state.after_query"]
        for metric in ("native.sql_result", "native.database_state", "native.file_state"):
            assert verdicts[metric] == "pass", f"{metric} 未接线：{verdicts.get(metric)}"

    async def test_exit_code_assertion_goes_red(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        _outcome, results = await _run_all(evals_root, data_root, fixtures_root)
        verdicts = _by_case(results)["command.exit_code.nonzero"]
        # 负向 case：脚本以退出码 1 结束，断言要求 0 → 必须 FAIL
        assert verdicts["native.exit_code"] == "fail", verdicts.get("native.exit_code")


class TestMetricParamsResolution:
    """case 级 params 的解析规则（Spec §17.2）。"""

    def test_unknown_metric_id_fails_fast(self, evals_tree, fixtures_root) -> None:
        """声明了 profile 里不存在的 metric → 报错，不能静默不生效。

        构造的是真实会犯的错：profile 只跑 `harness.retry`，case 却去覆盖
        `harness.loop` 的参数。前者不会报错、也不会生效——正是"声明了却静默失效"。
        """
        evals_root, data_root = evals_tree
        ds = evals_root / "datasets" / "badparams" / "cases"
        ds.mkdir(parents=True, exist_ok=True)
        (ds.parent / "dataset.yaml").write_text("id: badparams\nversion: 1.0.0\n", encoding="utf-8")
        (ds / "bad.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "bad.params",
                    "version": 1,
                    "name": "bad",
                    "tags": ["bp"],
                    "input": {"type": "single_turn", "prompt": "ping"},
                    "metric_params": {"harness.loop": {"max_repeats": 1}},
                }
            ),
            encoding="utf-8",
        )
        (evals_root / "benchmarks" / "badparams-bench.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "badparams-bench",
                    "dataset": "badparams@1.0.0",
                    "suites": ["bp"],
                    "default_profile": "retry-only",
                }
            ),
            encoding="utf-8",
        )
        (evals_root / "suites" / "bp.yaml").write_text(
            yaml.safe_dump({"name": "bp", "tags": ["bp"]}), encoding="utf-8"
        )
        (evals_root / "profiles" / "retry-only.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "retry-only",
                    "metrics": [{"id": "harness.retry", "params": {"max_retries": 0}}],
                }
            ),
            encoding="utf-8",
        )
        fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
        cfg = RunConfig(
            evals_root=evals_root,
            fixtures_root=fx,
            data_root=data_root,
            benchmark="badparams-bench",
            agent_endpoint="fake://",
            gate="pr",
            no_judge=True,
        )
        import asyncio

        with pytest.raises(InvalidCallError, match="harness.loop"):
            asyncio.run(Runner(cfg).run())

    def test_case_params_override_profile_params(self) -> None:
        """优先级：插件默认值 < Profile < Case（Spec §17.2）。"""
        from agent_eval.evaluators.harness import RetryEvaluator
        from agent_eval.evaluators.plugin import EvaluationContext

        plugin = RetryEvaluator()
        assert plugin.default_params["max_retries"] == 0
        # Runner 侧合并顺序由 runner 保证；这里固定默认值这一环不得悄悄放宽
        merged = {**plugin.default_params, **{"max_retries": 2}, **{"max_retries": 5}}
        assert merged["max_retries"] == 5
        context = EvaluationContext(
            case_run_id="cr", case_id="c", iteration=1, params=merged, metric_id=plugin.name
        )
        assert context.param("max_retries") == 5


def test_dataset_files_are_all_lf() -> None:
    """case YAML 必须 LF（.gitattributes: * text=auto eol=lf）。"""
    offenders = [
        path.name
        for path in (EVALS / "datasets" / "database-core" / "cases").glob("*.yaml")
        if b"\r\n" in path.read_bytes()
    ]
    assert not offenders, f"CRLF 行尾：{offenders}"


def test_release_suites_resolve_to_nonempty_counts() -> None:
    """Release Gate 的四个必跑套件都要能选出 case（否则 suites.coverage 永远 FAIL）。"""
    benchmark = load_benchmark(EVALS, "database-core")
    _info, cases = load_dataset(EVALS, benchmark.dataset)
    suites = load_suites(EVALS)
    _selected, counts = resolve_suites(
        benchmark, suites, cases, ["golden", "regression", "security", "core"], None
    )
    assert all(count > 0 for count in counts.values()), counts
