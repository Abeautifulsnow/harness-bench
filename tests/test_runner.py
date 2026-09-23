"""Runner 端到端测试（FakeAgentAdapter）：PASS/FLAKY/partial/exit code。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml

from agent_eval.adapters.base import AgentRequest, AgentSession
from agent_eval.adapters.fake import FakeAgentAdapter, ScriptTurn
from agent_eval.errors import EXIT_INFRA, InfraError
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


def test_invalid_benchmark_exits_3(evals_tree, fixtures_root) -> None:
    import asyncio

    from agent_eval.errors import InvalidCallError

    evals_root, data_root = evals_tree
    cfg = make_cfg(evals_root, data_root, fixtures_root)
    cfg.benchmark = "missing"
    with pytest.raises(InvalidCallError, match="not found"):
        asyncio.run(Runner(cfg).run())


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
