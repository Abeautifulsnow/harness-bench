"""HttpAgentAdapter 测试：MockTransport SSE + 连接失败语义。"""

from __future__ import annotations

import json

import httpx
import pytest

from agent_eval.adapters.base import AgentRequest, SessionContext
from agent_eval.adapters.http_adapter import HttpAgentAdapter
from agent_eval.errors import InfraError
from agent_eval.models.events import TraceEvent


def _sse_bytes(events: list[TraceEvent]) -> bytes:
    return b"".join(f"data: {json.dumps(e.model_dump(mode='json'))}\n\n".encode() for e in events)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock")


def _evt(eid: str, etype: str) -> TraceEvent:
    return TraceEvent(
        event_id=eid,
        trace_id="t",
        type=etype,
        timestamp="2026-09-23T11:00:00+08:00",
        data={},
    )


async def test_health_create_run_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/agent/sessions":
            return httpx.Response(200, json={"session_id": "sess_1"})
        if request.url.path.endswith("/run"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse_bytes([_evt("e1", "run.started"), _evt("e2", "run.finished")]),
            )
        return httpx.Response(404)

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    assert (await adapter.health_check()).ok is True
    session = await adapter.create_session(
        SessionContext(eval_run_id="r1", case_id="c1", iteration=1)
    )
    assert session.session_id == "sess_1"
    events = [e async for e in adapter.run(session, AgentRequest(message="hi"))]
    assert [e.type for e in events] == ["run.started", "run.finished"]


async def test_transport_error_maps_to_infra() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    assert (await adapter.health_check()).ok is False  # health 不抛异常

    with pytest.raises(InfraError, match="create_session"):
        await adapter.create_session(SessionContext(eval_run_id="r", case_id="c"))


async def test_http_error_on_run_maps_to_infra() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent/sessions":
            return httpx.Response(200, json={"session_id": "s"})
        return httpx.Response(500, text="boom")

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    session = await adapter.create_session(SessionContext(eval_run_id="r", case_id="c"))
    with pytest.raises(InfraError, match="500"):
        [e async for e in adapter.run(session, AgentRequest(message="hi"))]
