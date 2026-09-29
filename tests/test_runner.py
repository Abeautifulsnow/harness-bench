"""Runner 端到端测试（FakeAgentAdapter）：PASS/FLAKY/partial/exit code。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml

from agent_eval.adapters.base import AgentRequest, AgentSession, HealthStatus, SessionContext
from agent_eval.adapters.fake import FakeAgentAdapter, ScriptTurn
from agent_eval.errors import EXIT_INFRA, InfraError
from agent_eval.ids import new_id
from agent_eval.models.events import TraceEvent
from agent_eval.models.results import CaseStatus, Stability
from agent_eval.models.run import FailureSemantics, RunStatus
from agent_eval.runner.runner import RunConfig, Runner


def make_cfg(evals_root, data_root, fixtures_root, **kw) -> RunConfig:
    # evals_tree 已把 fixtures 拷到 tmp_path/fixtures；standalone fixture 则在 fixtures_standalone
    fx = fixtures_root
    if not fx.exists():
        fx = fx.parent / "fixtures"
    return RunConfig(
        evals_root=evals_root,
        fixtures_root=fx,
        data_root=data_root,
        benchmark="database-core",
        agent_endpoint="fake://",
        **kw,
    )


def add_scripted_dataset(evals_root: Path, cases: list[dict], *, profile: str = "default") -> str:
    """注入临时数据集 + suite + benchmark（写任意 case 定义，供回归用）。"""
    ds = evals_root / "datasets" / "scripted"
    (ds / "cases").mkdir(parents=True, exist_ok=True)
    (ds / "dataset.yaml").write_text("id: scripted\nversion: 1.0.0\n", encoding="utf-8")
    for case in cases:
        (ds / "cases" / f"{case['id']}.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")
    (evals_root / "benchmarks" / "scripted-bench.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "scripted-bench",
                "dataset": "scripted@1.0.0",
                "suites": ["s1"],
                "default_profile": profile,
            }
        ),
        encoding="utf-8",
    )
    (evals_root / "suites" / "s1.yaml").write_text(
        yaml.safe_dump({"name": "s1", "tags": ["scripted"]}), encoding="utf-8"
    )
    return "scripted-bench"


def scripted_cfg(evals_root: Path, data_root: Path, fixtures_root: Path, benchmark: str, **kw):
    fx = fixtures_root if fixtures_root.exists() else fixtures_root.parent / "fixtures"
    return RunConfig(
        evals_root=evals_root,
        fixtures_root=fx,
        data_root=data_root,
        benchmark=benchmark,
        agent_endpoint="fake://",
        **kw,
    )


async def test_smoke_run_stable_pass(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=3)
    outcome = await Runner(cfg).run()
    assert outcome.exit_code == 0
    assert outcome.verdict == "pass"
    assert outcome.status == RunStatus.completed.value
    echo = next(c for c in outcome.report["cases"] if c["case_id"] == "smoke.echo.basic")
    assert echo["stability"] == Stability.STABLE_PASS.value
    assert echo["pass_rate"] == 1.0


async def test_degradation_recorded_without_deepeval(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    outcome = await Runner(cfg).run()
    assert outcome.report["metric_degradations"] == {
        "agent.task_completion": "native.output_checks",
        "agent.tool_correctness": "native.tool_sequence",
    }
    # Raw trace 落盘（PRD §110）
    trace_files = list((data_root / "runs" / outcome.run_id / "traces").glob("*.jsonl"))
    assert trace_files, "raw trace jsonl must be persisted"
    events = [json.loads(line) for line in trace_files[0].read_text(encoding="utf-8").splitlines()]
    assert any(e["type"] == "run.started" for e in events)


async def test_negative_cases_yield_exit_1(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root)  # smoke+core 含负向用例
    outcome = await Runner(cfg).run()
    assert outcome.exit_code == 1
    assert outcome.verdict == "fail"
    guard = next(
        c for c in outcome.report["cases"] if c["case_id"] == "database.guard.forbidden_shell"
    )
    assert guard["pass_rate"] == 0.0
    # repeat=1 → 有效轮 <3 → 稳定性 UNKNOWN（Spec §3.1），但 blocking FAIL 仍判定 exit 1
    assert guard["stability"] == Stability.UNKNOWN.value


async def test_flaky_detection(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    benchmark = add_scripted_dataset(
        evals_root,
        [
            {
                "id": "scripted.one",
                "version": 1,
                "name": "scripted",
                "tags": ["scripted"],
                "input": {"type": "single_turn", "prompt": "flaky target"},
                "execution": {"timeout": 10, "repeat": 1},
                "expected": {"output": {"contains": ["QUERY COMPLETE"]}},
            }
        ],
    )
    cfg = scripted_cfg(evals_root, data_root, fixtures_root, benchmark, repeat=3, concurrency=1)
    runner = Runner(cfg)
    runner.adapter = FakeAgentAdapter(
        script_queue=[
            ScriptTurn(),
            ScriptTurn(output="sorry, cannot"),  # contains 断言失败
            ScriptTurn(),
        ]
    )
    outcome = await runner.run()
    case = outcome.report["cases"][0]
    assert case["stability"] == Stability.FLAKY.value
    assert case["pass_rate"] == pytest.approx(2 / 3)
    assert outcome.exit_code == 1  # 混有 blocking FAIL


async def test_turn_level_assertion_failure_blocks_case(evals_tree, fixtures_root) -> None:
    """回归 #I01：turn 级 expect 失败必须参与 case 终判（Spec §2.2 挂载点之间 AND）。

    修复前该判定只被写入 turn_results 而无消费方：case 报 PASS、exit 0。
    """
    evals_root, data_root = evals_tree
    benchmark = add_scripted_dataset(
        evals_root,
        [
            {
                "id": "scripted.multi",
                "version": 1,
                "name": "turn assert",
                "tags": ["scripted"],
                "input": {
                    "type": "multi_turn",
                    "turns": [
                        {
                            "user": "t1",
                            "expect": {"tools": {"required": ["never_called_tool"]}},
                        },
                        {"user": "t2"},
                    ],
                },
                "execution": {"timeout": 10, "repeat": 1},
                "expected": {"final": {"output": {"contains": ["QUERY COMPLETE"]}}},
            }
        ],
    )
    cfg = scripted_cfg(evals_root, data_root, fixtures_root, benchmark, concurrency=1)
    runner = Runner(cfg)
    runner.adapter = FakeAgentAdapter(
        script_queue=[ScriptTurn(), ScriptTurn(output="QUERY COMPLETE ok")]
    )
    outcome = await runner.run()

    assert outcome.verdict == "fail"
    assert outcome.exit_code == 1
    case = outcome.report["cases"][0]
    assert case["pass_rate"] == 0.0
    failures = case["blocking_failures"]
    assert [(f["metric"], f["mount"], f["turn"]) for f in failures] == [
        ("native.tool_sequence", "turn[1]", 1)
    ]
    _, results = runner.store.load_run(outcome.run_id)
    assert results[0].status == CaseStatus.FAIL


async def test_session_mount_points_are_both_evaluated(evals_tree, fixtures_root) -> None:
    """case 级 expected 与 session 级 expected.final 都要评测（修复 #I02 的另一面）。"""
    evals_root, data_root = evals_tree
    benchmark = add_scripted_dataset(
        evals_root,
        [
            {
                "id": "scripted.two_mounts",
                "version": 1,
                "name": "two mounts",
                "tags": ["scripted"],
                "input": {
                    "type": "multi_turn",
                    "turns": [{"user": "t1"}, {"user": "t2"}],
                },
                "execution": {"timeout": 10, "repeat": 1},
                # case 级断言要求不存在的工具 → 即便 final 层通过也必须 FAIL
                "expected": {"tools": {"required": ["phantom_tool"]}},
                "expected_final": {"output": {"contains": ["QUERY COMPLETE"]}},
            }
        ],
    )
    cfg = scripted_cfg(evals_root, data_root, fixtures_root, benchmark, concurrency=1)
    runner = Runner(cfg)
    runner.adapter = FakeAgentAdapter(
        script_queue=[ScriptTurn(), ScriptTurn(output="QUERY COMPLETE ok")]
    )
    outcome = await runner.run()

    mounts = {f["mount"] for f in outcome.report["cases"][0]["blocking_failures"]}
    assert mounts == {"expected"}
    assert outcome.exit_code == 1


class _BreakingAdapter(FakeAgentAdapter):
    """create_session 成功但 SSE 流中断 → INFRA_FAILURE（§46）。"""

    def run(self, session: AgentSession, request: AgentRequest) -> AsyncIterator:
        return self._break(session, request)

    async def _break(self, session, request) -> AsyncIterator:
        raise InfraError("connection reset mid-stream")
        yield  # pragma: no cover


async def test_infra_failure_makes_run_partial_exit_2(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    runner = Runner(cfg)
    runner.adapter = _BreakingAdapter()
    outcome = await runner.run()
    assert outcome.exit_code == EXIT_INFRA
    assert outcome.status == RunStatus.partial.value
    store_results = runner.store.load_run(outcome.run_id)[1]
    assert all(r.status == CaseStatus.ERROR for r in store_results)
    assert all(r.failure_semantics == FailureSemantics.INFRA for r in store_results)


async def test_agent_error_is_agent_failure_not_error(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    # 不加 tag 过滤：database.error.recovery（prompt 含 [fail]）在 core suite
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["negative"], repeat=1)
    runner = Runner(cfg)
    outcome = await runner.run()
    assert outcome.status == RunStatus.completed.value  # agent 失败不算 partial
    _, results = runner.store.load_run(outcome.run_id)
    error_case = next(r for r in results if r.case_id == "database.error.recovery")
    assert error_case.status == CaseStatus.FAIL
    assert error_case.failure_semantics == FailureSemantics.AGENT


async def test_multi_turn_session_lifecycle(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    cfg = make_cfg(
        evals_root,
        data_root,
        fixtures_root,
        tag_filter=["multi-turn"],
        repeat=2,
        concurrency=1,
    )
    runner = Runner(cfg)
    outcome = await runner.run()
    assert outcome.exit_code == 0
    adapter = runner.adapter
    assert isinstance(adapter, FakeAgentAdapter)
    assert len(adapter.created_sessions) == 2  # 2 iterations × 每 iteration 新 session
    _, results = runner.store.load_run(outcome.run_id)
    multi = [r for r in results if r.case_id == "database.query.multi_turn_refine"]
    assert all(len(r.turn_results) == 2 for r in multi)


# ---------------------------------------------------------- A1/A3/A4/E1 端到端


class InstrumentedAgent(FakeAgentAdapter):
    """探针适配器：health 观测面/模型、create_session 回执、方言事件、用量裁剪。

    其余行为与 FakeAgent 完全一致——每条测试只改变被验证的那一个变量。
    """

    def __init__(
        self,
        *,
        surface: dict[str, bool] | None = None,
        agent_model: str | None = None,
        receipt: bool | None = None,
        dialect_event: str | None = None,
        drop_output_tokens: bool = False,
    ) -> None:
        super().__init__()
        self.surface = surface
        self.agent_model = agent_model
        self.receipt = receipt
        self.dialect_event = dialect_event
        self.drop_output_tokens = drop_output_tokens
        self.created: list[SessionContext] = []

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            ok=True,
            detail="fake",
            observation_surface=dict(self.surface or {}),
            agent_model=self.agent_model,
        )

    async def create_session(self, context: SessionContext) -> AgentSession:
        self.created.append(context)
        session = await super().create_session(context)
        if self.receipt is not None:
            session.workdir_accessible = self.receipt
        return session

    async def _run(self, session, request) -> AsyncIterator[TraceEvent]:
        async for event in super()._run(session, request):
            if self.drop_output_tokens and event.type == "model.response":
                usage = event.data.get("usage")
                if isinstance(usage, dict):
                    # A3 半缺形态：协议只给输入侧（ai-chatbot 的 data-context-usage）
                    event.data["usage"] = {k: v for k, v in usage.items() if k != "output_tokens"}
            yield event
            if event.type == "run.started" and self.dialect_event:
                yield TraceEvent(
                    event_id=new_id("evt"),
                    trace_id=event.trace_id,
                    type=self.dialect_event,
                    data={},
                )


async def _run_instrumented(cfg: RunConfig, adapter: FakeAgentAdapter):
    runner = Runner(cfg)
    runner.adapter = adapter
    outcome = await runner.run()
    meta, results = runner.store.load_run(outcome.run_id)
    return outcome, meta, results


async def test_workdir_reaches_agent_per_iteration(evals_tree, fixtures_root) -> None:
    """A1 去程：fixture 沙箱 handle 经 SessionContext.extra 交给被测方，逐迭代独立。"""
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    adapter = InstrumentedAgent(receipt=True)
    outcome, meta, _ = await _run_instrumented(cfg, adapter)
    assert adapter.created
    assert all("workdir" in ctx.extra for ctx in adapter.created)
    assert len({ctx.extra["workdir"] for ctx in adapter.created}) == len(adapter.created)
    assert outcome.exit_code == 0
    assert meta.warnings == []  # 明确回执可达 → 无告警


async def test_workdir_receipt_false_is_infra(evals_tree, fixtures_root) -> None:
    """A1 回程：SUT 回执不可达 → InfraError（exit 2），把配置错与 agent 失败分开。"""
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    outcome, _, results = await _run_instrumented(cfg, InstrumentedAgent(receipt=False))
    assert outcome.status == RunStatus.partial.value
    assert outcome.exit_code == EXIT_INFRA
    assert results
    assert all(r.status == CaseStatus.ERROR for r in results)
    assert all(r.failure_semantics == FailureSemantics.INFRA for r in results)


async def test_missing_workdir_receipt_is_visible_warning(evals_tree, fixtures_root) -> None:
    """A1：回执缺失 = 未知——不默认视为可达，记账可见但不阻断。"""
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    outcome, meta, _ = await _run_instrumented(cfg, InstrumentedAgent(receipt=None))
    assert outcome.exit_code == 0  # 未知不改变判定
    assert any("WORKDIR RECEIPT MISSING" in w for w in outcome.aggregate.warnings)
    assert any("WORKDIR RECEIPT MISSING" in w for w in meta.warnings)


async def test_workdir_writer_makes_file_state_judgeable(evals_tree, fixtures_root) -> None:
    """A1 端到端：agent 侧读 workdir 写文件，file_state 判 pass/fail 而非 skipped。"""

    class WorkdirWriter(InstrumentedAgent):
        async def create_session(self, context: SessionContext) -> AgentSession:
            workdir = context.extra.get("workdir")
            if workdir:
                Path(workdir).mkdir(parents=True, exist_ok=True)
                (Path(workdir) / "agent_output.txt").write_text("agent was here", encoding="utf-8")
            return await super().create_session(context)

    evals_root, data_root = evals_tree
    case = {
        "id": "wd.file_state",
        "name": "workdir 透传端到端",
        "version": 1,
        "tags": ["scripted"],
        "input": {"type": "single_turn", "prompt": "write the file"},
        "environment": {"fixture": "sales_v2", "database": "sqlite"},
        "execution": {"timeout": 30, "repeat": 1},
        "expected": {
            "file_state": {"files": {"agent_output.txt": {"contains": ["agent was here"]}}}
        },
    }
    benchmark = add_scripted_dataset(evals_root, [case])
    cfg = scripted_cfg(
        evals_root, data_root, fixtures_root, benchmark, baseline_policy="NO_BASELINE"
    )
    outcome, _, results = await _run_instrumented(cfg, WorkdirWriter(receipt=True))
    positive = next(r for r in results if r.case_id == "wd.file_state")
    verdict = next(m for m in positive.all_metric_results if m.metric == "native.file_state")
    assert verdict.verdict == "pass"  # agent 写进沙箱的文件被断言看到

    # 负向：同一行为，断言"文件必须不存在" → 真 FAIL（断言面双向可红）。
    # 注意 add_scripted_dataset 是追加式：第二次调用后 dataset 含两个 case，
    # 断言必须按 case_id 收窄——按 metric id 合并（dict last-wins）会让结论
    # 依赖两 case 的并发完成顺序。
    case["id"] = "wd.file_state.negative"
    case["expected"] = {"file_state": {"absent": ["agent_output.txt"]}}
    benchmark = add_scripted_dataset(evals_root, [case])
    cfg = scripted_cfg(
        evals_root, data_root, fixtures_root, benchmark, baseline_policy="NO_BASELINE"
    )
    outcome, _, results = await _run_instrumented(cfg, WorkdirWriter(receipt=True))
    negative = next(r for r in results if r.case_id == "wd.file_state.negative")
    verdict = next(m for m in negative.all_metric_results if m.metric == "native.file_state")
    assert verdict.verdict == "fail"
    positive = next(r for r in results if r.case_id == "wd.file_state")
    assert positive.status == CaseStatus.PASS  # 正向 case 在同一 run 中照常成立
    assert outcome.exit_code == 1


async def test_usage_scope_full_when_both_sides_observed(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    _, meta, _ = await _run_instrumented(cfg, InstrumentedAgent())
    assert meta.token_usage_scope == "full"  # FakeAgent 的 model.response 带双侧 usage


async def test_usage_scope_partial_when_output_side_missing(evals_tree, fixtures_root) -> None:
    """A3 修订五：单侧观测的 run 记 partial 口径——它是基线守卫的比较依据。"""
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    _, meta, _ = await _run_instrumented(cfg, InstrumentedAgent(drop_output_tokens=True))
    assert meta.token_usage_scope == "partial"


async def test_health_reported_model_lands_in_meta(evals_tree, fixtures_root) -> None:
    """A4：SUT 自报的实际生效模型回填 agent_model，权威于 CLI 标签。"""
    evals_root, data_root = evals_tree
    cfg = make_cfg(
        evals_root,
        data_root,
        fixtures_root,
        tag_filter=["smoke"],
        repeat=1,
        agent_model="cli-label",
    )
    _, meta, _ = await _run_instrumented(cfg, InstrumentedAgent(agent_model="sut-effective-model"))
    assert meta.agent_model == "sut-effective-model"


async def test_unknown_event_type_warns_without_changing_verdict(evals_tree, fixtures_root) -> None:
    """E1 默认档：协议违约可见（计数 + warnings），verdict 与 exit code 不动。"""
    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1)
    adapter = InstrumentedAgent(dialect_event="mcp_call")
    outcome, meta, _ = await _run_instrumented(cfg, adapter)
    assert meta.protocol_violations.get("mcp_call", 0) >= 1
    assert any("mcp_call" in w for w in outcome.aggregate.warnings)
    assert outcome.verdict == "pass"
    assert outcome.exit_code == 0


async def test_strict_protocol_infra_fails_the_run(evals_tree, fixtures_root) -> None:
    """E1 升级档：strict_protocol 下未知事件类型 → INFRA（exit 2，run partial）。"""
    evals_root, data_root = evals_tree
    cfg = make_cfg(
        evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1, strict_protocol=True
    )
    outcome, meta, results = await _run_instrumented(
        cfg, InstrumentedAgent(dialect_event="mcp_call")
    )
    assert outcome.status == RunStatus.partial.value
    assert outcome.exit_code == EXIT_INFRA
    assert results
    assert all(r.failure_semantics == FailureSemantics.INFRA for r in results)
    assert meta.protocol_violations.get("mcp_call", 0) >= 1


def test_invalid_benchmark_exits_3(evals_tree, fixtures_root) -> None:
    import asyncio

    from agent_eval.errors import InvalidCallError

    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root)
    cfg.benchmark = "missing"
    with pytest.raises(InvalidCallError, match="not found"):
        asyncio.run(Runner(cfg).run())


@pytest.mark.real_judge
async def test_judge_phase_does_not_hold_agent_slot(evals_tree, fixtures_root, monkeypatch) -> None:
    """回归 #C01：Judge 阶段不得占用 agent 并发槽位（PRD §86）。

    用区间重叠做判定，不用墙钟阈值：concurrency=1 时，若 Judge 仍在 agent 槽位内，
    judge 区间不可能与任何 agent 区间重叠；分离后必然出现重叠。
    """
    import asyncio as _asyncio
    import time

    from agent_eval.evaluators import deepeval_adapter as adapter_mod

    timeline: list[tuple[str, float, float]] = []
    judge_sleep = 0.05
    agent_sleep = 0.05

    async def slow_evaluate(self, metric_id, threshold, trace):
        started = time.perf_counter()
        await _asyncio.sleep(judge_sleep)
        timeline.append(("judge", started, time.perf_counter()))
        return 1.0, "stubbed judge"

    monkeypatch.setattr(adapter_mod.DeepEvalCapabilityAdapter, "evaluate", slow_evaluate)
    monkeypatch.setattr(
        adapter_mod.DeepEvalCapabilityAdapter,
        "probe",
        lambda self: {f"agent.{name}": True for name in ("task_completion", "tool_correctness")},
    )

    evals_root, data_root = evals_tree
    cfg = make_cfg(
        evals_root, data_root, fixtures_root, tag_filter=["smoke"], repeat=1, concurrency=1
    )
    runner = Runner(cfg)

    class _TimedAdapter(FakeAgentAdapter):
        async def _run(self, session, request):
            started = time.perf_counter()
            try:
                async for event in super()._run(session, request):
                    yield event
            finally:
                timeline.append(("agent", started, time.perf_counter()))

    runner.adapter = _TimedAdapter(
        default=ScriptTurn(sleep_s=agent_sleep),
        rules={
            "30 天": ScriptTurn(
                sleep_s=agent_sleep,
                output="QUERY COMPLETE: mock agent result（已按最近 30 天过滤）",
            )
        },
    )
    outcome = await runner.run()
    assert outcome.exit_code == 0

    agents = [entry for entry in timeline if entry[0] == "agent"]
    judges = [entry for entry in timeline if entry[0] == "judge"]
    assert agents and judges
    overlapped = any(
        agent_start < judge_end and judge_start < agent_end
        for _, agent_start, agent_end in agents
        for _, judge_start, judge_end in judges
    )
    assert overlapped, "Judge 区间与所有 agent 区间互不重叠 → Judge 仍在 agent 槽位内"
