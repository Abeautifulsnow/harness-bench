"""OTel Trace JSON → 平台 TraceEvent 转换（§53 Production Trace Ingestion 第一片）。

输入遵循 OTel traces API 的 JSON 编码（protobuf-json 映射）：
    {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": "...", "spanId": "...", "parentSpanId"?: "...",
        "name": "...", "startTimeUnixNano": "171...", "endTimeUnixNano": "171...",
        "attributes": [{"key": "gen_ai.request.model", "value": {"stringValue": "..."}}]
    }]}]}]}

映射策略：**保真优先，不猜语义**。span 名不强行映射进 PRD §8 闭合词汇表——
生产侧的 span 命名属于被观测系统，改写反而制造假象；type 保留原名，
`otel.*` 归类字段另存。gen_ai / openinference 语义约定只做"提示字段"提取
（model / tool 名），供 UI 展示，不参与判定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_eval.errors import InvalidCallError

# gen_ai / openinference 语义约定里常见的"提示字段"（只提取展示，不做判定依据）。
_HINT_KEYS = ("gen_ai.request.model", "gen_ai.system", "gen_ai.tool.name", "tool.name")


def is_otel_payload(payload: dict) -> bool:
    return isinstance(payload, dict) and "resourceSpans" in payload


def convert_otel(payload: dict) -> list[dict]:
    """OTel JSON → TraceEvent 兼容的 dict 列表（供 ProductionStore 落盘）。"""

    if not is_otel_payload(payload):
        raise InvalidCallError("payload is not OTel trace JSON: missing 'resourceSpans'")
    events: list[dict] = []
    for resource in payload.get("resourceSpans") or []:
        service_name = _service_name(resource)
        for scope in resource.get("scopeSpans") or []:
            for span in scope.get("spans") or []:
                events.append(_convert_span(span, service_name))
    if not events:
        raise InvalidCallError("OTel payload contains no spans")
    return events


def _convert_span(span: dict, service_name: str | None) -> dict:
    for key in ("traceId", "spanId", "name", "startTimeUnixNano"):
        if not span.get(key):
            raise InvalidCallError(f"OTel span missing required field: {key}")
    attributes = _flatten_attributes(span.get("attributes"))
    start = _nanos_to_datetime(span["startTimeUnixNano"])
    end = _nanos_to_datetime(span["endTimeUnixNano"]) if span.get("endTimeUnixNano") else None
    hints = {key: attributes[key] for key in _HINT_KEYS if key in attributes}
    return {
        "event_id": str(span.get("spanId")),
        "trace_id": str(span["traceId"]),
        "parent_span_id": str(span["parentSpanId"]) if span.get("parentSpanId") else None,
        # 不映射进 §8 词汇表：生产 span 名是被观测系统的事实，保真保留。
        "type": str(span["name"]),
        "timestamp": start.isoformat(),
        "data": {
            "service": service_name,
            "duration_ms": _duration_ms(start, end),
            "kind": span.get("kind"),
            "status": str((span.get("status") or {}).get("code", "UNSET")),
            "attributes": attributes,
            **({"hints": hints} if hints else {}),
        },
    }


def _flatten_attributes(raw: Any) -> dict:
    out: dict = {}
    for item in raw or []:
        key = item.get("key")
        value = (item.get("value") or {}).get("stringValue")
        if key is None:
            continue
        out[str(key)] = value
    return out


def _service_name(resource: dict) -> str | None:
    for item in (resource.get("resource") or {}).get("attributes") or []:
        if item.get("key") == "service.name":
            return (item.get("value") or {}).get("stringValue")
    return None


def _nanos_to_datetime(nanos: str | int) -> datetime:
    try:
        seconds = int(nanos) / 1_000_000_000
    except (TypeError, ValueError) as exc:
        raise InvalidCallError(f"invalid unix nano timestamp: {nanos!r}") from exc
    return datetime.fromtimestamp(seconds, tz=UTC).astimezone()


def _duration_ms(start: datetime, end: datetime | None) -> float | None:
    if end is None:
        return None
    return round((end - start).total_seconds() * 1000, 3)
