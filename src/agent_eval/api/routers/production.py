"""Production Trace API（§53 第一片：摄取 + 只读查看）。

摄取是唯一的生产侧写入口：执行 token 门（§58.5）+ OTel/platform 双格式自动
识别。查看端点只读；Span Tree 与 Run Trace Viewer 共用同一个 TraceBuilder。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Query as fastapi_Query

from agent_eval.api.security import enforce_exec_token
from agent_eval.errors import InvalidCallError
from agent_eval.evaluators.online_eval import (
    load_eval_policies,
)
from agent_eval.execution.replay_job import ReplayRequestError, run_replay
from agent_eval.models.events import TraceEvent
from agent_eval.storage.production_store import ProductionStore
from agent_eval.trace.otel import convert_otel, is_otel_payload

router = APIRouter(tags=["production"])

_MAX_BATCH_BYTES = 8 * 1024 * 1024


def _store(request: Request) -> ProductionStore:
    return ProductionStore(request.app.state.workspace.data_root)


@router.post(
    "/production/traces",
    status_code=201,
    summary="摄取生产 Trace（OTel JSON 或平台 TraceEvent；token 门）",
    dependencies=[Depends(enforce_exec_token)],
)
async def ingest_production_trace(request: Request) -> dict:
    body = await request.body()
    if len(body) > _MAX_BATCH_BYTES:
        raise HTTPException(status_code=413, detail="payload exceeds 8 MiB batch limit")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise HTTPException(status_code=400, detail="body.cases must be a non-empty list")

    # 格式自动识别：OTel resourceSpans（可整体一段或每 case 一段）或平台事件。
    cases: list[dict] = []
    source = str(payload.get("source") or ("otel" if is_otel_payload(raw_cases[0]) else "platform"))
    for index, raw in enumerate(raw_cases):
        if is_otel_payload(raw):
            cases.append(
                {
                    "case_id": str(
                        (raw.get("case_id") if isinstance(raw, dict) else None) or f"otel-{index}"
                    ),
                    "events": convert_otel(raw),
                    "input": raw.get("input") if isinstance(raw, dict) else None,
                    "expected_output": (
                        raw.get("expected_output") if isinstance(raw, dict) else None
                    ),
                    "context": raw.get("context") if isinstance(raw, dict) else None,
                }
            )
        else:
            case_id = raw.get("case_id") if isinstance(raw, dict) else None
            events = raw.get("events") if isinstance(raw, dict) else None
            if not case_id or not isinstance(events, list):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"cases[{index}] needs case_id and events (platform) or OTel resourceSpans"
                    ),
                )
            try:
                parsed = [TraceEvent.model_validate(event) for event in events]
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"cases[{index}] has invalid TraceEvent: {exc}",
                ) from exc
            cases.append(
                {
                    "case_id": str(case_id),
                    "events": [event.model_dump(mode="json") for event in parsed],
                    # §61：显式评测字段（Online Eval 提取的第一优先级）
                    "input": raw.get("input") if isinstance(raw, dict) else None,
                    "expected_output": (
                        raw.get("expected_output") if isinstance(raw, dict) else None
                    ),
                    "context": raw.get("context") if isinstance(raw, dict) else None,
                }
            )

    try:
        trace_id = _store(request).save(
            source=source,
            cases=cases,
            endpoint=payload.get("endpoint"),
            model=payload.get("model"),
            trace_id=payload.get("trace_id"),
        )
    except InvalidCallError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"trace_id": trace_id, "cases": len(cases)}


@router.get(
    "/production/traces",
    summary="已摄取生产 Trace 列表（新→旧）",
)
async def list_production_traces(request: Request) -> list[dict]:
    return _store(request).list()


@router.get(
    "/production/traces/{trace_id}",
    summary="生产 Trace 详情（meta + case 清单）",
)
async def production_trace_detail(trace_id: str, request: Request) -> dict:
    try:
        meta = _store(request).get_meta(trace_id)
    except InvalidCallError:
        # review #I01：路径形态的 id 一律按"不存在"处理（404），不 500 不泄露。
        raise HTTPException(
            status_code=404, detail=f"unknown production trace: {trace_id}"
        ) from None
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown production trace: {trace_id}")
    return meta


@router.get(
    "/production/traces/{trace_id}/trace",
    summary="生产 Trace 的 Span Tree（与 Run Trace Viewer 同一构建器）",
)
async def production_trace_view(trace_id: str, request: Request) -> dict[str, Any]:
    from agent_eval.api.routers.runs import _trace_view_from_events

    store = _store(request)
    try:
        known = store.get_meta(trace_id) is not None
        events = store.load_events(trace_id, request.query_params.get("case_id"))
    except InvalidCallError:
        raise HTTPException(
            status_code=404, detail=f"unknown production trace: {trace_id}"
        ) from None
    if not known:
        raise HTTPException(status_code=404, detail=f"unknown production trace: {trace_id}")
    case_id = request.query_params.get("case_id")
    if not events:
        raise HTTPException(status_code=404, detail="no events for this trace/case")
    tree = _tree_by_parent(events, trace_id=trace_id, case_id=case_id)
    if tree is not None:
        return tree
    return _trace_view_from_events(events, trace_id=trace_id, case_id=case_id)


def _tree_by_parent(events: list[dict], *, trace_id: str, case_id: str | None) -> dict | None:
    """按显式 parent 结构直构 Span Tree（OTel span 天生带树，无需事件配对启发式）。

    平台事件流（§8 词汇、无稳定 parent）返回 None，回落 TraceBuilder。
    """

    from agent_eval.api.schemas import SpanNode

    by_id: dict[str, SpanNode] = {}
    for raw in events:
        event_id = str(raw.get("event_id") or "")
        if not event_id or event_id in by_id:
            continue
        data = raw.get("data") or {}
        by_id[event_id] = SpanNode(
            id=event_id,
            parent_span_id=raw.get("parent_span_id"),
            type=str(raw.get("type") or "span"),
            name=str(data.get("hints", {}).get("gen_ai.request.model") or raw.get("type") or ""),
            started_at=raw.get("timestamp"),
            duration_ms=data.get("duration_ms"),
            attributes=data if isinstance(data, dict) else {},
        )
    if not by_id:
        return None
    roots: list[SpanNode] = []
    for node in by_id.values():
        parent = by_id.get(node.parent_span_id) if node.parent_span_id else None
        if parent is not None and parent is not node:
            parent.children.append(node)
        else:
            roots.append(node)
    if not roots:
        return None
    root = roots[0] if len(roots) == 1 else _virtual_root(roots)
    return {
        "run_id": "",
        "case_id": case_id,
        "iteration": 0,
        "trace_id": str(events[0].get("trace_id") or trace_id),
        "span_count": len(by_id),
        "tokens": None,
        "root": root.model_dump(),
        "tool_sequence": [
            node.name for node in by_id.values() if str(node.type).startswith(("tool.", "mcp."))
        ],
    }


def _virtual_root(roots: list) -> Any:
    from agent_eval.api.schemas import SpanNode

    return SpanNode(
        id="virtual-root",
        type="agent",
        name=f"{len(roots)} roots",
        children=list(roots),
    )


@router.get(
    "/eval-policies",
    summary="生产评测策略列表（Definition Plane 资产，evals/eval-policies/*.yaml）",
)
async def list_eval_policies(request: Request) -> list[dict]:

    policies = load_eval_policies(request.app.state.workspace.evals_root)
    return [policy.model_dump() for policy in policies]


@router.post(
    "/production/traces/{trace_id}/evaluate",
    status_code=201,
    summary="对生产 Trace 跑 Online Eval（§61：参考无关 judge，token 门）",
    dependencies=[Depends(enforce_exec_token)],
)
async def evaluate_production_trace(trace_id: str, request: Request) -> dict:
    from agent_eval.evaluators.online_eval import extract_pair as _extract
    from agent_eval.evaluators.online_eval import run_online_evaluation as _run

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    policy_id = body.get("policy") if isinstance(body, dict) else None

    policies = {
        policy.id: policy for policy in load_eval_policies(request.app.state.workspace.evals_root)
    }
    policy = policies.get(str(policy_id))
    if policy is None:
        raise HTTPException(status_code=400, detail=f"unknown eval policy: {policy_id}")

    store = _store(request)
    try:
        meta = store.get_meta(trace_id)
    except InvalidCallError:
        raise HTTPException(
            status_code=404, detail=f"unknown production trace: {trace_id}"
        ) from None
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown production trace: {trace_id}")

    pairs = []
    unextractable: list[str] = []
    for case_id, entry in (meta.get("cases") or {}).items():
        pair = _extract(case_id, entry, store.load_events(trace_id, case_id))
        if pair is None:
            unextractable.append(case_id)
        else:
            pairs.append(pair)
    if not pairs:
        raise HTTPException(
            status_code=422,
            detail=(
                "no evaluable cases: production pairs need input+output "
                "(explicit at ingest or extractable from events)"
            ),
        )

    result = await _run(
        pairs=pairs,
        policy=policy,
        adapter=request.app.state.production_evaluator,
        unextractable_cases=unextractable,
    )
    store.save_evaluation(trace_id, result)
    return {
        "evaluation_id": result["evaluation_id"],
        "trace_id": trace_id,
        "summary": result["summary"],
        "rows": result["rows"],
    }


@router.get(
    "/production/monitors",
    summary="生产评测 Monitor 列表（Definition Plane + 运行台账合并视图）",
)
async def list_production_monitors(request: Request) -> list[dict]:
    return [view.model_dump() for view in request.app.state.production_monitors.monitor_views()]


@router.post(
    "/production/monitors/{monitor_id}/backfill",
    summary="对历史生产 Trace 显式回填 Online Eval（token 门；同步、有界）",
    dependencies=[Depends(enforce_exec_token)],
)
def backfill_production_monitor(
    monitor_id: str, request: Request, limit: int | None = fastapi_Query(default=None, ge=1)
) -> dict:
    """同步端点（FastAPI 线程池执行）：回填内部要为 judge 调用起事件循环，
    不能跑在 FastAPI 的主 loop 里（asyncio.run 会拒绝嵌套）。"""
    from agent_eval.errors import InvalidCallError

    try:
        return request.app.state.production_monitors.backfill(monitor_id, limit=limit)
    except InvalidCallError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.get(
    "/production/trends",
    summary="Online Eval 趋势：跨 trace 按 (日期, metric) 聚合 pass/fail/error/skipped",
)
async def production_eval_trends(request: Request, days: int = 30) -> list[dict]:
    from agent_eval.execution.production_monitors import online_eval_trend

    return online_eval_trend(_store(request), days=max(1, min(days, 365)))


@router.post(
    "/production/traces/{trace_id}/review-queue",
    summary="把一次 Online Eval 的 FAIL case 入 Review Queue（pending，token 门）",
    dependencies=[Depends(enforce_exec_token)],
)
async def enqueue_production_review_queue(trace_id: str, request: Request) -> dict:
    from agent_eval.review.production_queue import queue_production_failures

    try:
        body = await request.json()
    except ValueError:
        body = {}  # 空 body 合法：缺省取最新一次评测
    evaluation_id = body.get("evaluation_id") if isinstance(body, dict) else None

    store = _store(request)
    meta = _require_meta(store, trace_id)
    evaluation = _resolve_evaluation(store, trace_id, meta, evaluation_id)
    enqueued = queue_production_failures(
        request.app.state.workspace.review_store(), trace_id=trace_id, evaluation=evaluation
    )
    return {
        "trace_id": trace_id,
        "evaluation_id": evaluation.get("evaluation_id"),
        "enqueued": enqueued,
    }


@router.post(
    "/production/traces/{trace_id}/promote",
    summary="由 production 失败 case 生成回归 Case Draft（token 门；人工评审后落库）",
    dependencies=[Depends(enforce_exec_token)],
)
async def promote_production_case(trace_id: str, request: Request) -> dict:
    from agent_eval.evaluators.online_eval import extract_pair as _extract
    from agent_eval.failures.promote import build_draft_from_production, validate_assertions

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    case_id = str(body.get("case_id") or "") if isinstance(body, dict) else ""
    evaluation_id = body.get("evaluation_id") if isinstance(body, dict) else None
    if not case_id:
        raise HTTPException(status_code=400, detail="body.case_id is required")

    store = _store(request)
    meta = _require_meta(store, trace_id)
    if case_id not in (meta.get("cases") or {}):
        raise HTTPException(status_code=404, detail=f"unknown case in trace: {case_id}")
    evaluation = _resolve_evaluation(store, trace_id, meta, evaluation_id)
    failed = [
        row
        for row in evaluation.get("rows") or []
        if row.get("verdict") == "fail" and str(row.get("case_id")) == case_id
    ]
    if not failed:
        raise HTTPException(
            status_code=422,
            detail=f"case {case_id} has no failing row in evaluation "
            f"{evaluation.get('evaluation_id')}",
        )
    pair = _extract(case_id, meta["cases"][case_id], store.load_events(trace_id, case_id))
    if pair is None:
        raise HTTPException(
            status_code=422,
            detail="pair not extractable for this case (need input+output)",
        )
    draft = build_draft_from_production(
        trace_id,
        pair,
        evaluation_id=str(evaluation.get("evaluation_id")),
        failure_reason=str(failed[0].get("reason") or failed[0].get("metric") or ""),
    )
    invalid = validate_assertions(draft.suggested_assertions)
    if invalid:  # 不落不可用的 draft：断言 schema 校验必须过
        raise HTTPException(status_code=422, detail=f"invalid suggested assertions: {invalid}")
    request.app.state.workspace.draft_store().save(draft)
    return draft.as_dict()


def _require_meta(store: ProductionStore, trace_id: str) -> dict:
    try:
        meta = store.get_meta(trace_id)
    except InvalidCallError:
        raise HTTPException(
            status_code=404, detail=f"unknown production trace: {trace_id}"
        ) from None
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown production trace: {trace_id}")
    return meta


def _resolve_evaluation(
    store: ProductionStore, trace_id: str, meta: dict, evaluation_id: object
) -> dict:
    """指定 id 或缺省取最新一次评测；缺评测 → 422（先 evaluate 再入队/晋级）。"""

    if evaluation_id:
        evaluation = store.get_evaluation(trace_id, str(evaluation_id))
        if evaluation is None:
            raise HTTPException(status_code=404, detail=f"unknown evaluation: {evaluation_id}")
        return evaluation
    evaluations = store.list_evaluations(trace_id)
    if not evaluations:
        raise HTTPException(
            status_code=422,
            detail=f"trace {trace_id} has no online evaluation yet",
        )
    return evaluations[0]


@router.post(
    "/production/traces/{trace_id}/replay",
    summary="Trace Replay：脱敏后用 candidate agent 重放并与历史行为比较（token 门）",
    dependencies=[Depends(enforce_exec_token)],
)
async def replay_production_trace(trace_id: str, request: Request) -> dict:
    """同步入口（有界超时）：保留给 CI / 脚本；Web 走 /api/replay-runs 的 Job 路径。

    §63：两条入口调用的都是 ``execution/replay_job.run_replay``——本函数只负责
    请求体解析、agent 目标解析与错误码映射，"脱敏 / 重放 / 比较 / 落账"四处逻辑
    不再各有一份。契约不变：执行失败也返回 200 + record（status="error"）；前置
    条件不成立（未知 trace/case/policy、pair 不可提取）仍按 400/404/422 拒绝。
    """

    try:
        body = await request.json()
    except ValueError:
        body = {}  # 空 body 合法：全部走缺省
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    # 错误优先级是契约的一部分（既有测试钉着）：未知 trace 先于"缺 endpoint"报错，
    # 因此这里先做一次存在性检查——run_replay 内部还会再读一次 meta（幂等、廉价），
    # 但把 404 的判定放在解析请求体之前，语义才与同步端点既有行为一致。
    _require_meta(_store(request), trace_id)
    options = _parse_replay_options(body)
    endpoint, headers = _resolve_replay_target(body, request)
    raw_case = body.get("case_id")
    raw_policy = body.get("policy")
    try:
        return await run_replay(
            data_root=request.app.state.workspace.data_root,
            evals_root=request.app.state.workspace.evals_root,
            trace_id=trace_id,
            case_id=str(raw_case) if raw_case else None,
            endpoint=endpoint,
            headers=headers,
            sanitize=options["sanitize"],
            timeout_seconds=options["timeout_seconds"],
            policy_id=str(raw_policy) if raw_policy else None,
            judge_adapter=request.app.state.production_evaluator,
        )
    except ReplayRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _parse_replay_options(body: dict) -> dict:
    """请求体标量项解析与校验：非法输入一律 400（Spec §6.1 invalid-call 语义）。"""
    raw_timeout = body.get("timeout_seconds")
    if raw_timeout is None:
        timeout_seconds = 60.0
    else:
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="timeout_seconds must be a number"
            ) from None
    if timeout_seconds <= 0:
        raise HTTPException(status_code=400, detail="timeout_seconds must be positive")
    return {"sanitize": bool(body.get("sanitize", True)), "timeout_seconds": timeout_seconds}


def _resolve_replay_target(body: dict, request: Request) -> tuple[str, dict[str, str] | None]:
    """端点解析：显式 agent_endpoint 优先；agent_profile 走注册表，secret 只在
    服务端解析成 header（§27：浏览器拿不到凭证）。"""
    from agent_eval.execution.connections import load_agent_connections

    endpoint = body.get("agent_endpoint")
    headers: dict[str, str] | None = None
    if not endpoint and body.get("agent_profile"):
        profile = next(
            (
                candidate
                for candidate in load_agent_connections(request.app.state.workspace.evals_root)
                if candidate.id == str(body["agent_profile"])
            ),
            None,
        )
        if profile is None:
            raise HTTPException(
                status_code=404, detail=f"unknown agent profile: {body['agent_profile']}"
            )
        endpoint = profile.endpoint
        headers = profile.auth_headers() or None
    if not endpoint:
        raise HTTPException(
            status_code=400, detail="body.agent_endpoint or body.agent_profile is required"
        )
    return str(endpoint), headers


@router.get(
    "/production/replays",
    summary="Replay 记录列表（新→旧；可选 trace_id 过滤）",
)
async def list_production_replays(
    request: Request,
    trace_id: str | None = fastapi_Query(default=None),
) -> list[dict]:
    from agent_eval.storage.replay_store import ReplayStore

    records = ReplayStore(request.app.state.workspace.data_root).list()
    if trace_id is None:
        return records
    return [record for record in records if record.get("trace_id") == trace_id]


@router.get(
    "/production/replays/{replay_id}",
    summary="Replay 记录详情（含比较结论）",
)
async def get_production_replay(replay_id: str, request: Request) -> dict:
    from agent_eval.storage.replay_store import ReplayStore

    record = ReplayStore(request.app.state.workspace.data_root).get(replay_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown replay: {replay_id}")
    return record


@router.get(
    "/production/traces/{trace_id}/evaluations",
    summary="生产 Trace 的历次 Online Eval 结果（新→旧）",
)
async def list_production_evaluations(trace_id: str, request: Request) -> list[dict]:
    store = _store(request)
    try:
        return store.list_evaluations(trace_id)
    except InvalidCallError:
        raise HTTPException(
            status_code=404, detail=f"unknown production trace: {trace_id}"
        ) from None


