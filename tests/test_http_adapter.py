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


async def test_create_session_carries_extra_and_parses_receipt() -> None:
    """A1：extra 原样透传进请求 metadata；可达性回执从**响应体**解析。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent/sessions":
            captured["metadata"] = json.loads(request.content.decode())["metadata"]
            return httpx.Response(200, json={"session_id": "s1", "workdir_accessible": True})
        return httpx.Response(404)

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    session = await adapter.create_session(
        SessionContext(
            eval_run_id="r1",
            case_id="c1",
            iteration=2,
            extra={"workdir": "E:/sandbox/iter1"},
        )
    )
    assert captured["metadata"]["workdir"] == "E:/sandbox/iter1"
    assert captured["metadata"]["eval_run_id"] == "r1"
    assert session.workdir_accessible is True


async def test_create_session_receipt_missing_or_malformed_is_unknown() -> None:
    """回执缺失/非布尔 = 未知（None），由 runner 按"未知"记账，不默认视为可达。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent/sessions":
            return httpx.Response(200, json={"session_id": "s1", "workdir_accessible": "yes"})
        return httpx.Response(404)

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    session = await adapter.create_session(SessionContext(eval_run_id="r", case_id="c"))
    assert session.workdir_accessible is None


async def test_health_parses_observation_surface_and_model() -> None:
    """A2/A4：观测面能力表与实际生效模型在 health 阶段整份解析。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "observation_surface": {"retry": False, "tool.call": True},
                    "agent_model": "sut-effective-model",
                },
            )
        return httpx.Response(404)

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    health = await adapter.health_check()
    assert health.ok is True
    assert health.observation_surface == {"retry": False, "tool.call": True}
    assert health.agent_model == "sut-effective-model"


async def test_health_without_surface_fields_defaults_to_undeclared() -> None:
    """旧形状 /health（只有 status）→ 空表 = 未声明 = 按具备处理，模型为 None。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    health = await adapter.health_check()
    assert health.observation_surface == {}
    assert health.agent_model is None


async def test_health_non_2xx_body_lands_in_detail() -> None:
    """B1：health 非 2xx 时 body 里的原因（unreachable / license-invalid）必须进 detail——
    先 raise_for_status 会把 shim 给运维的排查信息吞掉。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                503, json={"status": "license-invalid", "detail": "upstream license state"}
            )
        return httpx.Response(404)

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    health = await adapter.health_check()
    assert health.ok is False
    assert "license-invalid" in health.detail
    assert "503" in health.detail
