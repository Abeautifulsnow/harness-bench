"""Trace Replay 驱动（P1-3：生产问题 → 脱敏 Case → candidate 重放 → 与历史比较）。

对齐语义（评估 2026-10-09 P1-3；与 Online Eval 的分工见 §61 记录）：
- Online Eval 回答"生产输出本身好不好"；Replay 回答"**换现在的 agent 重来一次
  会怎样**"。比较的两侧是：original（历史 pair）与 replay（同题重跑）。
- 比较的确定性部分：工具序列 Jaccard + 输出文本是否变化。judge 部分**可选**，
  且只在给了 eval policy 时对两侧用同一 policy 打分（apples-to-apples）；
  没有就不编造分数。
- 重放事件的落账形态是 ProductionStore 里一条 ``source="replay"`` 的 trace
  （meta.origin 指回原 trace）——Viewer / Online Eval 的全部既有能力直接复用。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from agent_eval.adapters import AgentRequest, SessionContext, open_adapter
from agent_eval.errors import InfraError
from agent_eval.evaluators.online_eval import (
    EvalPair,
    EvalPolicy,
    _output_from_events,
    _tools_from_events,
    run_online_evaluation,
)


async def execute_replay(
    *,
    replay_id: str,
    prompt: str,
    endpoint: str,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
    case_id: str = "replay",
) -> list[dict]:
    """把 prompt 发给 endpoint，收集完整事件流（TraceEvent 兼容 dict + case_id）。

    传输层失败 / agent 上报失败 / 超时都归 InfraError 语义族——重放执行不产出
    "评测 FAIL"，它要么拿到一次完整执行，要么如实报平台侧失败。
    """
    adapter = open_adapter(endpoint, headers=headers)
    health = await adapter.health_check()
    if not health.ok:
        raise InfraError(f"replay agent endpoint unhealthy: {health.detail}")
    session = await adapter.create_session(
        SessionContext(eval_run_id=f"replay:{replay_id}", case_id=case_id)
    )
    events: list[dict] = []
    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in adapter.run(session, AgentRequest(message=prompt, stream=True)):
                events.append({**event.model_dump(mode="json"), "case_id": case_id})
    except TimeoutError:
        with contextlib.suppress(Exception):  # best-effort 清理（adapter 层约定）
            await adapter.cancel(session)
        raise InfraError(f"replay timed out after {timeout_seconds}s") from None
    return events


async def compare_with_original(
    *,
    original_pair: EvalPair,
    prompt: str,
    replay_events: list[dict],
    policy: EvalPolicy | None = None,
    adapter: object | None = None,
) -> dict:
    """历史行为 vs 重放行为：工具序列 + 输出文本；给 policy 时加双侧 judge 分。"""

    original_tools = list(original_pair.tools_called)
    replay_tools = _tools_from_events(replay_events)
    a, b = set(original_tools), set(replay_tools)
    union = a | b
    jaccard = 0.0 if not union else 1.0 - len(a & b) / len(union)
    replay_output = _output_from_events(replay_events)
    comparison: dict = {
        "tool_sequence_original": original_tools,
        "tool_sequence_replay": replay_tools,
        "tool_sequence_jaccard": round(jaccard, 6),
        "output_original": original_pair.actual_output,
        "output_replay": replay_output,
        "output_changed": (replay_output or "") != (original_pair.actual_output or ""),
    }
    if policy is not None and adapter is not None:
        if replay_output is None:
            # 拿不到重放输出就不评：编造空输出会得出"质量崩了"的假结论
            comparison["evaluation_note"] = "replay output missing; judge skipped"
            return comparison
        replay_pair = EvalPair(
            case_id=original_pair.case_id,
            input=prompt,
            actual_output=replay_output,
            tools_called=replay_tools,
        )
        original_eval = await run_online_evaluation(
            pairs=[original_pair], policy=policy, adapter=adapter, unextractable_cases=[]
        )
        replay_eval = await run_online_evaluation(
            pairs=[replay_pair], policy=policy, adapter=adapter, unextractable_cases=[]
        )
        comparison["evaluation_original"] = original_eval["summary"]["metrics"]
        comparison["evaluation_replay"] = replay_eval["summary"]["metrics"]
    return comparison


def replay_wall_clock_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
