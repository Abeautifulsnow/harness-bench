"""PRD §44 Harness-specific Evaluators（Spec §17）。

两条主线：

1. **每个插件都必须两个方向都能判**：命中 → FAIL、合规 → PASS。只测 FAIL 方向会把
   "恒判 fail 的 evaluator" 当成正确实现（PRD §44 明确要求双向），只测 PASS 方向
   则退回"恒 PASS 占位"的老问题。
2. **观测面缺失时必须 skipped，不得凭空 pass**：MCP 授权集 / 期望路由未声明时判
   fail 是冤枉、判 pass 是假信号，所以两条都用 ``skipped``（blocking=False）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_eval.evaluators.harness import (
    HARNESS_EVALUATORS,
    LoopEvaluator,
    MCPPermissionEvaluator,
    RetryEvaluator,
    SubAgentRoutingEvaluator,
    longest_consecutive_run,
)
from agent_eval.evaluators.plugin import EvaluationContext
from agent_eval.evaluators.registry import METRIC_REGISTRY, PLUGINS
from agent_eval.models.events import TraceEvent
from agent_eval.models.results import CaseStatus, ToolCallRecord
from agent_eval.models.spans import TraceSpan
from agent_eval.runner.runner import RunConfig, Runner

REPO = Path(__file__).resolve().parents[1]


def _ctx(**kw) -> EvaluationContext:
    base = {"case_run_id": "cr_1", "case_id": "c1", "iteration": 1}
    base.update(kw)
    return EvaluationContext(**base)


def _retry_events(count: int) -> list[TraceEvent]:
    return [
        TraceEvent(
            event_id=f"evt_r{index}",
            trace_id="t1",
            type="retry",
            data={"reason": f"attempt {index} transient"},
        )
        for index in range(count)
    ]


def _tool_spans(names: list[str]) -> list[TraceSpan]:
    from datetime import datetime

    now = datetime.now().astimezone()
    return [
        TraceSpan(
            id=f"span_t{index}",
            trace_id="t1",
            type="tool",
            name=name,
            started_at=now,
            finished_at=now,
        )
        for index, name in enumerate(names)
    ]


class TestRetryEvaluator:
    async def test_within_limit_passes(self) -> None:
        plugin = RetryEvaluator()
        metric = await plugin.evaluate(
            _ctx(events=_retry_events(1), params={"max_retries": 2}, metric_id=plugin.name)
        )
        assert metric.verdict == "pass"
        assert metric.metadata["retries"] == 1

    async def test_over_limit_fails(self) -> None:
        plugin = RetryEvaluator()
        metric = await plugin.evaluate(
            _ctx(events=_retry_events(2), params={"max_retries": 0}, metric_id=plugin.name)
        )
        assert metric.verdict == "fail"
        assert metric.score == 0.0
        assert "transient" in metric.reason  # 最近一次重试原因进入 reason，便于归因

    async def test_default_param_is_strict_zero(self) -> None:
        """缺省 ``max_retries=0``：没有声明就不许重试，不该默认放行。"""
        plugin = RetryEvaluator()
        metric = await plugin.evaluate(
            _ctx(events=_retry_events(1), params={}, metric_id=plugin.name)
        )
        assert metric.verdict == "fail"


class TestLoopEvaluator:
    async def test_consecutive_repeat_fails(self) -> None:
        plugin = LoopEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                spans=_tool_spans(["execute_sql"] * 5),
                params={"max_repeats": 3},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "fail"
        assert metric.metadata["tool"] == "execute_sql"
        assert metric.metadata["longest_repeat"] == 5

    async def test_varied_tools_pass(self) -> None:
        plugin = LoopEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                spans=_tool_spans(["schema", "query", "query", "format"]),
                params={"max_repeats": 3},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "pass"
        assert metric.score == 1.0

    async def test_alternating_tools_are_not_a_loop(self) -> None:
        """A,B,A,B 是正常读写交替，判成循环会让指标失去信誉（Spec §11 准入原则）。"""
        plugin = LoopEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                spans=_tool_spans(["execute_sql", "format"] * 4),
                params={"max_repeats": 2},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "pass"


class TestLongestConsecutiveRun:
    @pytest.mark.parametrize(
        ("sequence", "expected"),
        [
            ([], (0, None)),
            (["a"], (1, "a")),
            (["a", "a", "b", "a"], (2, "a")),
            (["a", "b", "c"], (1, "a")),
            (["a", "b", "b", "b"], (3, "b")),
        ],
    )
    def test_shapes(self, sequence: list[str], expected: tuple[int, str | None]) -> None:
        assert longest_consecutive_run(sequence) == expected


class TestMCPPermissionEvaluator:
    async def test_undeclared_allowed_is_skipped(self) -> None:
        """没有声明授权集就没有依据：skipped 而非 pass（否则是假信号）。"""
        plugin = MCPPermissionEvaluator()
        metric = await plugin.evaluate(
            _ctx(mcp_calls=[ToolCallRecord(name="exfil_server")], params={}, metric_id=plugin.name)
        )
        assert metric.verdict == "skipped"
        assert metric.blocking is False

    async def test_authorized_call_passes(self) -> None:
        plugin = MCPPermissionEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                mcp_calls=[ToolCallRecord(name="db_tools"), ToolCallRecord(name="db_tools")],
                params={"allowed": ["db_tools"]},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "pass"

    async def test_unauthorized_call_fails(self) -> None:
        plugin = MCPPermissionEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                mcp_calls=[ToolCallRecord(name="db_tools"), ToolCallRecord(name="exfil_server")],
                params={"allowed": ["db_tools"]},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "fail"
        assert metric.metadata["unauthorized"] == ["exfil_server"]

    async def test_allowed_and_forbidden_are_different_surfaces(self) -> None:
        """白名单只读 ``mcp_calls``：普通 tool 调出不构成越权。"""
        plugin = MCPPermissionEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                tool_calls=[ToolCallRecord(name="exfil_server")],
                mcp_calls=[],
                params={"allowed": ["db_tools"]},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "pass"


def _subagent_spans(names: list[str]) -> list[TraceSpan]:
    from datetime import datetime

    now = datetime.now().astimezone()
    return [
        TraceSpan(
            id=f"span_s{index}",
            trace_id="t1",
            type="subagent",
            name=name,
            started_at=now,
            finished_at=now,
        )
        for index, name in enumerate(names)
    ]


class TestSubAgentRoutingEvaluator:
    async def test_undeclared_expected_is_skipped(self) -> None:
        plugin = SubAgentRoutingEvaluator()
        metric = await plugin.evaluate(
            _ctx(spans=_subagent_spans(["researcher"]), params={}, metric_id=plugin.name)
        )
        assert metric.verdict == "skipped"
        assert metric.blocking is False

    async def test_expected_route_passes(self) -> None:
        plugin = SubAgentRoutingEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                spans=_subagent_spans(["researcher"]),
                params={"expected": ["researcher"]},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "pass"

    async def test_missing_route_fails(self) -> None:
        plugin = SubAgentRoutingEvaluator()
        metric = await plugin.evaluate(
            _ctx(
                spans=_subagent_spans(["researcher"]),
                params={"expected": ["researcher", "writer"]},
                metric_id=plugin.name,
            )
        )
        assert metric.verdict == "fail"
        assert metric.metadata["missing"] == ["writer"]

    async def test_unexpected_route_fails_unless_allowed(self) -> None:
        plugin = SubAgentRoutingEvaluator()
        spans = _subagent_spans(["researcher", "improviser"])
        strict = await plugin.evaluate(
            _ctx(spans=spans, params={"expected": ["researcher"]}, metric_id=plugin.name)
        )
        assert strict.verdict == "fail"
        assert strict.metadata["unexpected"] == ["improviser"]

        lenient = await plugin.evaluate(
            _ctx(
                spans=spans,
                params={"expected": ["researcher"], "allow_extra": True},
                metric_id=plugin.name,
            )
        )
        assert lenient.verdict == "pass"


class TestRegistration:
    def test_builtins_are_registered(self) -> None:
        for plugin in HARNESS_EVALUATORS:
            assert PLUGINS[plugin.name] is plugin
            assert METRIC_REGISTRY[plugin.name].provider == "harness"

    def test_every_registered_metric_is_deterministic(self) -> None:
        """harness 插件不依赖外部 SDK：--no-judge 不得把它们一起关掉（Spec §17.1）。"""
        from agent_eval.evaluators.registry import DETERMINISTIC_PROVIDERS

        assert "harness" in DETERMINISTIC_PROVIDERS


# ---------------------------------------------------------------- 端到端

PROFILE = {
    "name": "harness-check",
    "metrics": [
        {"id": "harness.retry", "params": {"max_retries": 0}, "blocking": True},
        {"id": "harness.loop", "params": {"max_repeats": 3}},
    ],
}


def _harness_cfg(
    evals_root: Path,
    data_root: Path,
    fixtures_root: Path,
    cases: list[dict],
    *,
    profile_name: str = "harness-check",
    profile: dict | None = None,
    **kw,
) -> RunConfig:
    ds = evals_root / "datasets" / "harness"
    (ds / "cases").mkdir(parents=True, exist_ok=True)
    (ds / "dataset.yaml").write_text("id: harness\nversion: 1.0.0\n", encoding="utf-8")
    for case in cases:
        (ds / "cases" / f"{case['id']}.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")
    (evals_root / "benchmarks" / "harness-bench.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "harness-bench",
                "dataset": "harness@1.0.0",
                "suites": ["s1"],
                "default_profile": profile_name,
            }
        ),
        encoding="utf-8",
    )
    (evals_root / "suites" / "s1.yaml").write_text(
        yaml.safe_dump({"name": "s1", "tags": ["hx"]}), encoding="utf-8"
    )
    (evals_root / "profiles" / f"{profile_name}.yaml").write_text(
        yaml.safe_dump({"name": profile_name, **(profile or PROFILE)}), encoding="utf-8"
    )
    fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
    return RunConfig(
        evals_root=evals_root,
        fixtures_root=fx,
        data_root=data_root,
        benchmark="harness-bench",
        agent_endpoint="fake://",
        # pr gate 不声明必跑套件；release gate 会因本临时数据集没有 golden/regression/security
        # 而正确地判 suites.coverage FAIL——那是另一条规则，不混进本文件的断言
        gate="pr",
        **kw,
    )


def _hcase(case_id: str, prompt: str) -> dict:
    return {
        "id": case_id,
        "version": 1,
        "name": case_id,
        "tags": ["hx"],
        "input": {"type": "single_turn", "prompt": prompt},
        "execution": {"timeout": 15, "repeat": 1},
    }


async def _run(evals_root, data_root, fixtures_root, cases, **kw):
    cfg = _harness_cfg(evals_root, data_root, fixtures_root, cases, **kw)
    runner = Runner(cfg)
    outcome = await runner.run()
    _, results = runner.store.load_run(outcome.run_id)
    return outcome, results


def _harness_metrics(results) -> dict[str, str]:
    return {
        m.metric: m.verdict
        for r in results
        for m in r.all_metric_results
        if m.metric.startswith("harness.")
    }


class TestRunnerIntegration:
    async def test_retries_make_case_fail_when_blocking(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        outcome, results = await _run(
            evals_root, data_root, fixtures_root, [_hcase("hx.retry", "[retry] 再查一次。")]
        )
        assert _harness_metrics(results)["harness.retry"] == "fail"
        assert outcome.exit_code == 1
        assert outcome.verdict == "fail"

    async def test_clean_run_passes_all_plugins(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        outcome, results = await _run(
            evals_root, data_root, fixtures_root, [_hcase("hx.clean", "查一下客户。")]
        )
        metrics = _harness_metrics(results)
        assert metrics == {"harness.retry": "pass", "harness.loop": "pass"}
        assert outcome.exit_code == 0

    async def test_non_blocking_failure_does_not_fail_case(self, evals_tree, fixtures_root) -> None:
        """``harness.loop`` 未声明 blocking：命中只记 FAIL，不拦门禁。"""
        evals_root, data_root = evals_tree
        outcome, results = await _run(
            evals_root, data_root, fixtures_root, [_hcase("hx.loop", "[loop] 一直重试同一个查询。")]
        )
        assert _harness_metrics(results)["harness.loop"] == "fail"
        assert outcome.exit_code == 0

    async def test_no_judge_still_runs_harness_plugins(self, evals_tree, fixtures_root) -> None:
        """PRD §86 只要求 judge 与 agent 并发分离，不是关掉确定性评测。"""
        evals_root, data_root = evals_tree
        outcome, results = await _run(
            evals_root,
            data_root,
            fixtures_root,
            [_hcase("hx.retry", "[retry] 再查一次。")],
            no_judge=True,
        )
        assert "harness.retry" in _harness_metrics(results)
        assert outcome.exit_code == 1

    async def test_plugin_params_come_from_profile(self, evals_tree, fixtures_root) -> None:
        """同一插件、不同 params → 不同判决：这是 params 存在的理由（Spec §17.2）。"""
        evals_root, data_root = evals_tree
        _, strict = await _run(
            evals_root, data_root, fixtures_root, [_hcase("hx.p", "[retry] 再查一次。")]
        )
        metrics = [m for r in strict for m in r.all_metric_results if m.metric == "harness.retry"]
        assert metrics[0].metadata["max_retries"] == 0.0

    async def test_plugin_crash_is_evaluation_failure(self, evals_tree, fixtures_root) -> None:
        """插件崩溃不得静默变成 pass（PRD §46 EVALUATION_FAILURE）。"""
        from agent_eval.evaluators.plugin import EvaluatorPlugin
        from agent_eval.evaluators.registry import PLUGINS, register_plugin

        class Boom(EvaluatorPlugin):
            name = "harness.boom"
            description = "test-only exploding plugin"

            async def evaluate(self, context):  # type: ignore[no-untyped-def]
                raise RuntimeError("kaboom")

        boom = Boom()
        register_plugin(boom, default_threshold=None)
        try:
            evals_root, data_root = evals_tree
            _, results = await _run(
                evals_root,
                data_root,
                fixtures_root,
                [_hcase("hx.boom", "随便问。")],
                profile_name="harness-boom",
                profile={"metrics": [{"id": "harness.boom"}]},
            )
            broken = [r for r in results if r.status == CaseStatus.ERROR]
            assert broken, "插件异常必须让 CaseRun 变成 error，而不是 pass"
            assert any("harness.boom" in (r.error or "") for r in broken)
        finally:
            PLUGINS.pop("harness.boom", None)
            METRIC_REGISTRY.pop("harness.boom", None)
