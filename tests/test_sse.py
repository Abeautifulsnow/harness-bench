"""SSE 流解析测试：跨 chunk 分帧、注释、多 data 行、坏负载。"""

from __future__ import annotations

import json

import pytest

from agent_eval.adapters.sse import parse_sse_payload, parse_sse_stream
from agent_eval.errors import InfraError


def _event_bytes(seq: int, etype: str = "run.started") -> bytes:
    payload = {
        "event_id": f"evt_{seq}",
        "trace_id": "t1",
        "parent_span_id": None,
        "type": etype,
        "timestamp": "2026-09-23T11:00:00+08:00",
        "data": {},
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _collect(chunks: list[bytes]) -> list:
    async def gen():
        for c in chunks:
            yield c

    return [e async for e in parse_sse_stream(gen())]


async def test_split_across_chunks() -> None:
    whole = _event_bytes(1) + _event_bytes(2, "run.finished")
    mid = len(whole) // 2
    events = await _collect([whole[:mid], whole[mid:]])
    assert [e.type for e in events] == ["run.started", "run.finished"]


async def test_comments_and_keepalives_ignored() -> None:
    raw = b": keepalive\n\n" + _event_bytes(1)
    events = await _collect([raw])
    assert len(events) == 1


async def test_multiline_data_joined() -> None:
    payload = json.dumps(
        {
            "event_id": "evt_x",
            "trace_id": "t",
            "type": "retry",
            "timestamp": "2026-09-23T11:00:00+08:00",
            "data": {"n": 1},
        }
    )
    raw = f"data: {payload[:20]}\ndata: {payload[20:]}\n\n".encode()
    events = await _collect([raw])
    assert events[0].type == "retry"


def test_invalid_json_raises_infra() -> None:
    with pytest.raises(InfraError):
        parse_sse_payload("data: not-json")


def test_schema_violation_raises_infra() -> None:
    with pytest.raises(InfraError):
        parse_sse_payload('data: {"type": 123}')  # type 非法


async def test_trailing_event_without_blank_line() -> None:
    events = await _collect([_event_bytes(1).rstrip(b"\n")])
    assert len(events) == 1
