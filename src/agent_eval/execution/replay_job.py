"""Trace Replay 执行体（设计文档 §62/§63）——同步端点与 Job 路径的唯一实现。

§63.2（为什么抽出来）：重放此前只有同步入口（``POST /production/traces/{id}/replay``，
有界超时）。Job 化之后同一段编排有两个调用方——同步端点回 record，Job 路径把失败
翻译成 ``failure_kind``——拆成两份实现必然漂移，而脱敏 / 落账 / 比较这三步任何一处
分叉都会得到"同一个重放在两个入口结论不同"的假事实。

失败语义（两个入口共用的一条）：执行失败时记录**先落盘**（``status="error"`` +
``error`` 文本），随后按 ``failure_mode`` 分叉——同步端点拿 record 当响应体（既有
契约不变），Job 路径让异常穿透到 service 按 ``failure_kind`` 收口。两边都不会得到
"失败了但报告里看不出"的静默 200。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.evaluators.online_eval import (
    EvalPair,
    EvalPolicy,
    extract_pair,
    load_eval_policies,
)
from agent_eval.ids import new_id
from agent_eval.runner.replay import compare_with_original, execute_replay
from agent_eval.storage.production_store import ProductionStore
from agent_eval.storage.replay_store import ReplayStore
from agent_eval.trace.sanitize import sanitize_text


class ReplayRequestError(InvalidCallError):
    """重放的前置条件不成立（trace/case/policy/pair 层面）→ 调用方按 status_code 呈现。

    刻意继承 ``InvalidCallError``：Job 路径的 ``_failure_kind`` 因此天然把它归为
    ``INVALID_REQUEST``（exit 3 语义：定义/证据树里没有这个东西），不需要在 service
    里为它开一个分支；同步端点则读出 ``status_code`` 保留既有的 400/404/422 契约。
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class ReplayFailureMode(StrEnum):
    """执行失败的呈现方式（§63.2：两条入口各取一种，见 ``run_replay``）。

    显式枚举而不是布尔开关（§63.7 review）：布尔名 ``raise_on_failure`` 的否定式
    （"不 raise"到底返回什么）读不出来，第三种调用方很容易沿用默认值然后惊讶。
    """

    RETURN_RECORD = "return_record"  # 同步端点：200 + status="error" 的 record
    RAISE = "raise"  # Job 路径：记录落盘后上抛，service 翻译成 failure_kind


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


async def run_replay(
    *,
    data_root: Path,
    evals_root: Path,
    trace_id: str,
    endpoint: str,
    case_id: str | None = None,
    headers: dict[str, str] | None = None,
    sanitize: bool = True,
    timeout_seconds: float = 60.0,
    policy_id: str | None = None,
    judge_adapter: object | None = None,
    on_progress: Callable[[dict], None] | None = None,
    failure_mode: ReplayFailureMode = ReplayFailureMode.RETURN_RECORD,
) -> dict:
    """脱敏 → 重放 → 与历史比较 → 落账（ReplayStore）；返回 record。

    ``failure_mode`` 决定**执行阶段**失败（agent 连不上、超时、比较崩）的呈现：
    ``RETURN_RECORD`` = 返回 status="error" 的 record（同步端点的既有契约，200 +
    record）；``RAISE`` = 记录已落盘后原样上抛（Job 路径据此翻译成 failure_kind）。
    前置条件类失败（未知 trace/case/policy、pair 不可提取）两种入口都上抛——
    它们不是"这次重放失败了"，而是"这次请求不成立"，用 record 表达会掩盖区别。
    """
    store, resolved_case, pair, policy = _resolve_scope(
        data_root=data_root,
        evals_root=evals_root,
        trace_id=trace_id,
        case_id=case_id,
        policy_id=policy_id,
    )
    replay_store = ReplayStore(data_root)
    prompt, redactions = (
        sanitize_text(pair.input or "") if sanitize else (pair.input or "", 0)
    )
    replay_id = new_id("rp")
    started = time.monotonic()
    record = _initial_record(
        replay_id=replay_id,
        trace_id=trace_id,
        case_id=resolved_case,
        sanitize=sanitize,
        redactions=redactions,
        source_input=pair.input,
        prompt=prompt,
        endpoint=endpoint,
    )
    _emit(
        on_progress,
        {
            "type": "replay.prepared",
            "replay_id": replay_id,
            "case_id": resolved_case,
            "redactions": redactions,
        },
    )
    try:
        replay_trace_id, comparison = await _execute_and_compare(
            store=store,
            pair=pair,
            trace_id=trace_id,
            case_id=resolved_case,
            replay_id=replay_id,
            prompt=prompt,
            endpoint=endpoint,
            headers=headers,
            timeout_seconds=timeout_seconds,
            policy=policy,
            judge_adapter=judge_adapter,
            on_progress=on_progress,
        )
        record.update(
            status="completed",
            replay_trace_id=replay_trace_id,
            comparison=comparison,
            duration_ms=int((time.monotonic() - started) * 1000),
            finished_at=now_iso(),
        )
    except Exception as exc:  # noqa: BLE001 - 失败也留完整现场（落账后按 mode 分叉）
        record.update(
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
            finished_at=now_iso(),
        )
        replay_store.save(record)
        if failure_mode is ReplayFailureMode.RAISE:
            raise
        return record
    replay_store.save(record)
    return record


def _resolve_scope(
    *,
    data_root: Path,
    evals_root: Path,
    trace_id: str,
    case_id: str | None,
    policy_id: str | None,
) -> tuple[ProductionStore, str, EvalPair, EvalPolicy | None]:
    """前置解析：trace 存在 → case 归属 → pair 可提取 → policy 合法。

    四步任何一步不成立都抛 ``ReplayRequestError``（带 HTTP 语义的状态码）；
    返回值即"这次重放可以开始执行"的全部前提。
    """
    store = ProductionStore(data_root)
    try:
        meta = store.get_meta(trace_id)
    except InvalidCallError as exc:  # 非法 trace_id 形状
        raise ReplayRequestError(str(exc), status_code=404) from exc
    if meta is None:
        raise ReplayRequestError(f"unknown production trace: {trace_id}", status_code=404)
    resolved_case = _resolve_case(case_id, meta, trace_id)
    pair = extract_pair(
        resolved_case,
        meta["cases"][resolved_case],
        store.load_events(trace_id, resolved_case),
    )
    if pair is None:
        raise ReplayRequestError(
            f"pair not extractable for case {resolved_case!r}: need input+output",
            status_code=422,
        )
    policy = _resolve_policy(policy_id, evals_root)
    return store, resolved_case, pair, policy


async def _execute_and_compare(
    *,
    store: ProductionStore,
    pair: EvalPair,
    trace_id: str,
    case_id: str,
    replay_id: str,
    prompt: str,
    endpoint: str,
    headers: dict[str, str] | None,
    timeout_seconds: float,
    policy: EvalPolicy | None,
    judge_adapter: object | None,
    on_progress: Callable[[dict], None] | None,
) -> tuple[str, dict]:
    """执行一次重放：落 trace（source=replay，origin 指回原 trace）→ 与历史比较。"""
    events = await execute_replay(
        replay_id=replay_id,
        prompt=prompt,
        endpoint=endpoint,
        headers=headers,
        timeout_seconds=timeout_seconds,
        case_id=case_id,
    )
    replay_trace_id = store.save(
        source="replay",
        cases=[{"case_id": case_id, "events": events}],
        endpoint=endpoint,
        origin=trace_id,
    )
    _emit(on_progress, {"type": "replay.executed", "replay_trace_id": replay_trace_id})
    comparison = await compare_with_original(
        original_pair=pair,
        prompt=prompt,
        replay_events=events,
        policy=policy,
        adapter=(judge_adapter if policy is not None else None),
    )
    _emit(
        on_progress,
        {
            "type": "replay.compared",
            "output_changed": comparison.get("output_changed"),
            "tool_sequence_jaccard": comparison.get("tool_sequence_jaccard"),
        },
    )
    return replay_trace_id, comparison


def _initial_record(
    *,
    replay_id: str,
    trace_id: str,
    case_id: str,
    sanitize: bool,
    redactions: int,
    source_input: str | None,
    prompt: str,
    endpoint: str,
) -> dict:
    """ReplayStore 记录的初始形态：status 恒从 "error" 起步（成功由 update 覆盖）。"""
    return {
        "replay_id": replay_id,
        "trace_id": trace_id,
        "case_id": case_id,
        "status": "error",
        "error": None,
        "sanitize": {"enabled": sanitize, "redactions": redactions},
        "source_input": source_input,
        "prompt": prompt,
        "endpoint": endpoint,
        "replay_trace_id": None,
        "comparison": None,
        "created_at": now_iso(),
    }


def _resolve_case(case_id: str | None, meta: dict, trace_id: str) -> str:
    """case 解析：显式指定优先；单 case trace 缺省取唯一一个，多 case 必须显式。"""

    resolved = case_id
    if resolved is None:
        cases = meta.get("cases") or {}
        if len(cases) == 1:
            resolved = next(iter(cases))
        else:
            raise ReplayRequestError(
                f"trace {trace_id} has multiple cases; case_id is required"
            )
    resolved = str(resolved)
    if resolved not in (meta.get("cases") or {}):
        raise ReplayRequestError(
            f"unknown case in trace: {resolved}", status_code=404
        )
    return resolved


def _resolve_policy(policy_id: str | None, evals_root: Path) -> EvalPolicy | None:
    """policy 引用解析：声明了不存在的 policy 是配置错（不是"静默不评"）。"""

    if not policy_id:
        return None
    policies = {p.id: p for p in load_eval_policies(evals_root)}
    policy = policies.get(str(policy_id))
    if policy is None:
        raise ReplayRequestError(f"unknown eval policy: {policy_id}")
    return policy


def _emit(on_progress: Callable[[dict], None] | None, event: dict) -> None:
    if on_progress is not None:
        on_progress(event)
