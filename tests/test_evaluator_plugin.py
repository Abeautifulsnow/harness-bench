"""Evaluator Plugin SDK 契约（PRD §43/§109.4，Spec §17）。

核心断言是 **"加插件 = 只调用 register_plugin"**：第三方新增 Evaluator 不得修改
``runner.py``（PRD §109.4）。这个证明必须落在测试里——否则"可扩展"只是文档承诺，
下一次有人图省事在 Runner 里加 if-else 也没人拦得住。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_eval.errors import InvalidCallError
from agent_eval.evaluators.plugin import (
    EvaluationContext,
    EvaluatorPlugin,
    describe_limit,
    ratio_score,
)
from agent_eval.evaluators.registry import (
    METRIC_REGISTRY,
    PLUGINS,
    available_providers_for,
    provider_for,
    register_plugin,
    resolve_metric,
    run_plugin,
)
from agent_eval.models.events import TraceEvent
from agent_eval.models.profile import MetricSpec
from agent_eval.models.results import MetricResultModel
from agent_eval.runner.runner import RunConfig, Runner

REPO = Path(__file__).resolve().parents[1]


class _Echo(EvaluatorPlugin):
    """最小合规插件：把输出长度当成判定对象（只为测试契约）。"""

    name = "harness.echo"
    description = "test-only plugin returning a fixed verdict"
    default_params = {"verdict": "pass"}

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        return context.result(
            str(context.param("verdict", "pass")),
            score=1.0,
            reason="fixed verdict for contract test",
            metadata={"params_seen": sorted(context.params)},
        )


class _Renaming(EvaluatorPlugin):
    """违约插件：返回值自报成别的 metric（含平台保留命名空间）。"""

    name = "harness.renaming"

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        result = context.result("fail", reason="pretends to be native")
        return result.model_copy(update={"metric": "native.status"})


@pytest.fixture()
def cleanup_plugins():
    """注册表是进程级全局：测试注册完必须撤掉，否则污染其余用例的 provider 判定。"""
    added: list[str] = []
    yield added
    for name in added:
        PLUGINS.pop(name, None)
        METRIC_REGISTRY.pop(name, None)


class TestContextContract:
    async def test_result_is_minted_by_platform(self) -> None:
        ctx = EvaluationContext(case_run_id="cr_1", case_id="c", iteration=1, metric_id="harness.x")
        metric = ctx.result("fail", score=0.25, reason="r", metadata={"k": "v"})
        assert metric.case_run_id == "cr_1"
        assert metric.metric == "harness.x"  # 平台填，不由插件填
        assert metric.evaluator == "harness"
        assert metric.metadata == {"mount": "harness", "k": "v"}

    async def test_blocking_defaults_to_profile_not_verdict(self) -> None:
        """插件不自行决定阻断：默认 False，由 Runner 按 Profile 覆写。"""
        ctx = EvaluationContext(case_run_id="cr_1", case_id="c", iteration=1, metric_id="harness.x")
        assert ctx.result("fail").blocking is False
        assert ctx.result("error").blocking is False
        assert ctx.result("fail", blocking=True).blocking is True

    def test_observation_helpers(self) -> None:
        from datetime import datetime

        from agent_eval.models.spans import TraceSpan

        now = datetime.now().astimezone()
        ctx = EvaluationContext(
            case_run_id="cr_1",
            case_id="c",
            iteration=1,
            events=[
                TraceEvent(event_id="e1", trace_id="t", type="retry", data={}),
                TraceEvent(event_id="e2", trace_id="t", type="retry", data={}),
                TraceEvent(event_id="e3", trace_id="t", type="tool.call", data={}),
            ],
            spans=[
                TraceSpan(
                    id="s1", trace_id="t", type="tool", name="a", started_at=now, finished_at=now
                )
            ],
        )
        assert len(ctx.events_of("retry")) == 2
        assert ctx.spans_of("tool")[0].name == "a"
        assert ctx.tool_sequence() == ["a"]


class TestRegistration:
    def test_namespace_is_enforced(self, cleanup_plugins) -> None:
        """``security.*`` 是不可被 judge 覆盖的 Hard Gate，绝不允许外部占用。"""

        class Bad(EvaluatorPlugin):
            name = "security.mine"

            async def evaluate(self, context):  # type: ignore[no-untyped-def]
                return context.result("pass")

        with pytest.raises(InvalidCallError):
            register_plugin(Bad())

    def test_custom_namespace_is_also_rejected(self, cleanup_plugins) -> None:
        """``custom.*`` 是留给用户 GEval 的，插件走 harness.*（Spec §7.1）。"""

        class Bad(EvaluatorPlugin):
            name = "custom.mine"

            async def evaluate(self, context):  # type: ignore[no-untyped-def]
                return context.result("pass")

        with pytest.raises(InvalidCallError):
            register_plugin(Bad())

    def test_duplicate_name_is_rejected(self, cleanup_plugins) -> None:
        first = _Echo()
        register_plugin(first)
        cleanup_plugins.append(first.name)
        with pytest.raises(InvalidCallError):
            register_plugin(_Echo())  # 同名不同实例：必须拒绝，不能悄悄替换

    def test_registration_makes_metric_resolvable(self, cleanup_plugins) -> None:
        """注册是唯一动作：注册后 provider / 可用性 / 无 judge 语义自动跟上。"""
        plugin = _Echo()
        register_plugin(plugin, default_threshold=0.5)
        cleanup_plugins.append(plugin.name)

        spec = MetricSpec(id="harness.echo")
        assert provider_for(spec) == "harness"
        assert available_providers_for(spec) is True
        entry = METRIC_REGISTRY["harness.echo"]
        assert entry.default_threshold == 0.5
        # harness 是确定性 provider：--no-judge 不得把它一起关掉
        assert resolve_metric(spec, {}, no_judge=True) == ("harness.echo", None)

    def test_unregistered_plugin_fails_fast(self, cleanup_plugins) -> None:
        from agent_eval.errors import MetricUnavailableError

        class Ghost(EvaluatorPlugin):
            name = "harness.ghost"

            async def evaluate(self, context):  # type: ignore[no-untyped-def]
                return context.result("pass")

        PLUGINS.pop("harness.ghost", None)
        METRIC_REGISTRY.pop("harness.ghost", None)
        assert available_providers_for(MetricSpec(id="harness.ghost")) is False
        with pytest.raises(MetricUnavailableError):
            resolve_metric(MetricSpec(id="harness.ghost"), {}, no_judge=False)


class TestRunPluginGuard:
    async def test_renaming_is_an_error_not_a_silent_pass(self) -> None:
        """插件自报别的 metric 会让报告与 Gate 认到不存在/越权的指标，必须判 error。"""
        ctx = EvaluationContext(
            case_run_id="cr_1", case_id="c", iteration=1, metric_id="harness.renaming"
        )
        metric = await run_plugin(_Renaming(), ctx)
        assert metric.verdict == "error"
        assert metric.metric == "harness.renaming"  # 平台口径，不采纳插件的自报
        assert "native.status" in metric.reason

    async def test_compliant_plugin_passes_through(self) -> None:
        ctx = EvaluationContext(
            case_run_id="cr_1", case_id="c", iteration=1, metric_id="harness.echo"
        )
        metric = await run_plugin(_Echo(), ctx)
        assert metric.verdict == "pass"
        assert metric.evaluator == "harness"


class TestScoreHelpers:
    @pytest.mark.parametrize(
        ("limit", "actual", "expected"),
        [(3, 0, 1.0), (3, 3, 1.0), (3, 6, 0.5), (0, 5, 0.0), (3, 2, 1.0)],
    )
    def test_ratio_score(self, limit: float, actual: float, expected: float) -> None:
        assert ratio_score(limit, actual) == expected

    def test_describe_limit_states_both_sides(self) -> None:
        text = describe_limit(3, 5, "retries")
        assert "5" in text and "3" in text


# ---------------------------------------------------------------- 端到端：Runner 零改动

PLUGIN_SOURCE = '''\
"""第三方插件（测试用）：只依赖公开 SDK。"""

from __future__ import annotations

from agent_eval.evaluators.plugin import EvaluationContext, EvaluatorPlugin
from agent_eval.models.results import MetricResultModel


class KeywordEvaluator(EvaluatorPlugin):
    """判定最终输出是否包含声明的关键词（params.keyword）。"""

    name = "harness.keyword"
    description = "输出必须包含 params.keyword"
    default_params = {"keyword": ""}

    async def evaluate(self, context: EvaluationContext) -> MetricResultModel:
        keyword = str(context.param("keyword", ""))
        text = context.final_output or ""
        if keyword and keyword in text:
            return context.result("pass", score=1.0, reason=f"输出包含 {keyword!r}")
        return context.result("fail", score=0.0, reason=f"输出缺少 {keyword!r}")
'''


async def _run(evals_root: Path, data_root: Path, fixtures_root: Path, *, keyword: str):
    case = {
        "id": "sdk.keyword",
        "version": 1,
        "name": "sdk.keyword",
        "tags": ["sdk"],
        "input": {"type": "single_turn", "prompt": "30 天内的客户。"},
        "execution": {"timeout": 15, "repeat": 1},
    }
    ds = evals_root / "datasets" / "sdk"
    (ds / "cases").mkdir(parents=True, exist_ok=True)
    (ds / "dataset.yaml").write_text("id: sdk\nversion: 1.0.0\n", encoding="utf-8")
    (ds / "cases" / "sdk.keyword.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")
    (evals_root / "benchmarks" / "sdk-bench.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sdk-bench",
                "dataset": "sdk@1.0.0",
                "suites": ["sdk"],
                "default_profile": "sdk",
            }
        ),
        encoding="utf-8",
    )
    (evals_root / "suites" / "sdk.yaml").write_text(
        yaml.safe_dump({"name": "sdk", "tags": ["sdk"]}), encoding="utf-8"
    )
    (evals_root / "profiles" / "sdk.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sdk",
                "metrics": [
                    {"id": "harness.keyword", "params": {"keyword": keyword}, "blocking": True}
                ],
            }
        ),
        encoding="utf-8",
    )
    fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
    cfg = RunConfig(
        evals_root=evals_root,
        fixtures_root=fx,
        data_root=data_root,
        benchmark="sdk-bench",
        agent_endpoint="fake://",
        gate="pr",
    )
    runner = Runner(cfg)
    outcome = await runner.run()
    _, results = runner.store.load_run(outcome.run_id)
    metrics = {
        m.metric: m.verdict
        for r in results
        for m in r.all_metric_results
        if m.metric.startswith("harness.")
    }
    return outcome, metrics


class TestRunnerNeedsNoChange:
    """PRD §109.4：新增 Evaluator 不得修改主 Runner。"""

    def test_runner_source_does_not_know_about_plugins(self) -> None:
        """Runner 里不得出现任何具体插件名：它只按 provider 分派。"""
        source = (REPO / "src" / "agent_eval" / "runner" / "runner.py").read_text(encoding="utf-8")
        for plugin in PLUGINS:
            assert plugin not in source

    async def test_external_plugin_lands_in_run_result(
        self, evals_tree, fixtures_root, tmp_path, cleanup_plugins
    ) -> None:
        """把插件写在仓库之外、只 register_plugin，run 里就能看到它的 metric。"""
        evals_root, data_root = evals_tree
        module_dir = tmp_path / "third_party"
        module_dir.mkdir()
        # 动态加载写在临时目录里的插件模块（第三方视角：不装包、不进 src/）
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "third_party_plugin", module_dir / "keyword_evaluator.py"
        )
        assert spec is not None and spec.loader is not None
        (module_dir / "keyword_evaluator.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        plugin = module.KeywordEvaluator()
        register_plugin(plugin)
        cleanup_plugins.append(plugin.name)

        outcome, metrics = await _run(evals_root, data_root, fixtures_root, keyword="30 天")
        assert metrics == {"harness.keyword": "pass"}
        assert outcome.exit_code == 0

        outcome, metrics = await _run(evals_root, data_root, fixtures_root, keyword="去年同期")
        assert metrics == {"harness.keyword": "fail"}
        assert outcome.exit_code == 1  # params + blocking 都经 Profile 生效
