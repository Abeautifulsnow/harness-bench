"""A2/E2：观测面能力协商——"看不到"必须判 skipped，不能当"满足"。

change-plan §1 A2 / §5 E2：SUT 不上报某个事件面时（如 ai-chatbot 的 provider
重试只写日志、从不发 retry 事件），依赖它的插件若照常求值，`0 <= 0` 的恒 pass
就是假信号。修法分两段：

- 留痕段：能力表在 health 阶段整份上报，带 `event:` 前缀进 metric_capability_snapshot；
- 判定段：表进 EvaluationContext，`required_events` 缺失的插件由 run_plugin
  统一判 skipped（不走 resolve_metric——它的既有语义是 fallback/exit 3）。
"""

from __future__ import annotations

from agent_eval.adapters.base import HealthStatus
from agent_eval.adapters.fake import FakeAgentAdapter
from agent_eval.evaluators.harness import (
    ContextCompactionEvaluator,
    MCPPermissionEvaluator,
    RetryEvaluator,
)
from agent_eval.evaluators.plugin import EvaluationContext
from agent_eval.evaluators.registry import run_plugin
from agent_eval.models.events import TraceEvent
from agent_eval.runner.runner import RunConfig, Runner


def _ctx(**kw) -> EvaluationContext:
    base = {"case_run_id": "cr_1", "case_id": "c1", "iteration": 1}
    base.update(kw)
    return EvaluationContext(**base)


def _events(*types: str) -> list[TraceEvent]:
    return [
        TraceEvent(event_id=f"evt_{index}", trace_id="t1", type=etype, data={})
        for index, etype in enumerate(types)
    ]


class TestRunPluginObservationGate:
    async def test_missing_surface_is_skipped_not_pass(self) -> None:
        """核心验收：SUT 声明无 retry 观测面 → skipped（blocking=False），不是 pass。"""
        result = await run_plugin(
            RetryEvaluator(), _ctx(metric_id="harness.retry", observation_surface={"retry": False})
        )
        assert result.verdict == "skipped"
        assert result.blocking is False
        assert result.metadata["skipped_reason"] == "observation_unavailable"
        assert result.metadata["missing_events"] == ["retry"]

    async def test_declared_surface_still_fails_when_threshold_exceeded(self) -> None:
        """反向用例：能力表声明"具备"时，超阈值照样 FAIL——防止修成恒 skipped。"""
        ctx = _ctx(
            metric_id="harness.retry",
            observation_surface={"retry": True},
            events=_events("retry", "retry"),
        )
        result = await run_plugin(RetryEvaluator(), ctx)
        assert result.verdict == "fail"

    async def test_absent_from_surface_is_treated_as_available(self) -> None:
        """表里查不到声明 → 按"具备"处理：既有测试与 FakeAgent 路径行为不变。"""
        ctx = _ctx(metric_id="harness.retry", events=_events("retry", "retry"))
        assert (await run_plugin(RetryEvaluator(), ctx)).verdict == "fail"
        # 整表为空（未声明任何东西）同理：0 次重试 <= 上限 0 → pass，与现状一致
        assert (await run_plugin(RetryEvaluator(), _ctx(metric_id="harness.retry"))).verdict == (
            "pass"
        )

    async def test_mcp_permission_skips_when_mcp_surface_missing(self) -> None:
        """声明了 allowed 也救不了观测面缺失：先 skipped，不拿空越权集合判 pass。"""
        ctx = _ctx(
            metric_id="harness.mcp_permission",
            observation_surface={"mcp.call": False},
            params={"allowed": ["db_tools"]},
        )
        result = await run_plugin(MCPPermissionEvaluator(), ctx)
        assert result.verdict == "skipped"
        assert result.metadata["skipped_reason"] == "observation_unavailable"

    async def test_compaction_skips_when_pair_surface_missing(self) -> None:
        """started/finished 必须成对观测：任一侧缺失即整条不可判。"""
        ctx = _ctx(
            metric_id="harness.context_compaction",
            observation_surface={"context.compaction.started": False},
            params={"max_compactions": 3},
        )
        result = await run_plugin(ContextCompactionEvaluator(), ctx)
        assert result.verdict == "skipped"


class _SurfaceAdapter(FakeAgentAdapter):
    """health 阶段上报观测面表的 FakeAgent（其余行为不变，供 runner 端到端）。"""

    def __init__(self, surface: dict[str, bool]) -> None:
        super().__init__()
        self._surface = surface
        self.health_calls = 0

    async def health_check(self) -> HealthStatus:
        self.health_calls += 1
        return HealthStatus(ok=True, detail="fake", observation_surface=dict(self._surface))


async def _run(evals_root, data_root, fixtures_root, adapter: FakeAgentAdapter):
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
    runner.adapter = adapter
    outcome = await runner.run()
    meta, results = runner.store.load_run(outcome.run_id)
    return outcome, meta, results


async def test_runner_end_to_end_surface_flows_to_plugins(evals_tree, fixtures_root) -> None:
    """时序验收：表在 health 阶段采集（快照留痕）并到达插件的 EvaluationContext。"""
    evals_root, data_root = evals_tree
    surface = {
        "retry": False,
        "tool.call": False,
        "mcp.call": False,
        "skill.loaded": False,
        "subagent.started": False,
        "subagent.finished": False,
        "context.compaction.started": False,
        "context.compaction.finished": False,
    }
    adapter = _SurfaceAdapter(surface)
    outcome, meta, results = await _run(evals_root, data_root, fixtures_root, adapter)
    assert adapter.health_calls == 1  # run 级事实，整份一次拿到
    # 留痕段：event: 前缀进能力快照，与 probe() 的 metric id 键空间区分
    for event_name, flag in surface.items():
        assert meta.metric_capability_snapshot[f"event:{event_name}"] is flag
    # 判定段：harness.retry 判 skipped 且带 skipped_reason，不是 0<=0 的 pass
    retry = [m for r in results for m in r.all_metric_results if m.metric == "harness.retry"]
    assert retry, "smoke profile 声明了 harness.retry，必须有产出"
    assert all(m.verdict == "skipped" for m in retry)
    assert all(m.metadata.get("skipped_reason") == "observation_unavailable" for m in retry)
    # skipped 不阻断：run 照常收场（反向 FAIL 已由单元测试覆盖）
    assert outcome.exit_code == 0
