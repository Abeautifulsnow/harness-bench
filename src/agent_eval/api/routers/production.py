"""Production Trace API（§53 第一片：摄取 + 只读查看）。

摄取是唯一的生产侧写入口：执行 token 门（§58.5）+ OTel/platform 双格式自动
识别。查看端点只读；Span Tree 与 Run Trace Viewer 共用同一个 TraceBuilder。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_eval.api.security import enforce_exec_token
from agent_eval.errors import InvalidCallError
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
