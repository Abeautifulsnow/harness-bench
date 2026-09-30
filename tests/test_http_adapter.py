"""HttpAgentAdapter 测试：MockTransport SSE + 连接失败语义。"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


# --------------------------------------------------------------- 流式读超时


def _slow_sse_server(delay: float) -> tuple[ThreadingHTTPServer, str]:
    """起一个真实 HTTP 服务：SSE 响应在发出头之后**静默 delay 秒**才给首个事件。

    用它而不是 MockTransport，是因为要验的正是"两个 chunk 之间的等待"——
    MockTransport 一次性返回整个 body，构造不出这段静默。
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # quiet
            pass

        def do_GET(self) -> None:  # noqa: N802
            self._json({"session_id": "sess_slow", "workdir_accessible": True})

        def do_POST(self) -> None:  # noqa: N802
            # 必须先读干请求体：keep-alive 下未消费的 body 会被 HTTP/1.1 解析器
            # 当成下一条请求的请求行（表现为 501 Unsupported method，而服务端
            # 根本没收到第二次请求——排查一次踩坑的现场记录）。
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path == "/api/agent/sessions":
                self._json({"session_id": "sess_slow", "workdir_accessible": True})
                return
            body = b""
            for event in (_evt("e1", "run.started"), _evt("e2", "run.finished")):
                body += f"data: {json.dumps(event.model_dump(mode='json'))}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            time.sleep(delay)  # 头已发出，静默：读超时若存在就会在这里触发
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def _json(self, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


async def test_stream_survives_silence_longer_than_the_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一轮能跑多久由 case 的预算决定，不由传输层决定（联调实测缺陷的护栏）。

    红的方式（修之前）：`DEFAULT_TIMEOUT` 的 30s 读上限被套在流式调用上，
    于是"两个 chunk 之间静默过久"被判成 **INFRA_FAILURE（exit 2）**——
    一次正常的慢被报成基础设施故障，而 case 声明的 timeout 永远轮不到生效。
    这里把默认值缩到 0.2s 放大同一根因，服务端静默 0.6s：读侧若继承默认值，
    这个流必然断在静默里。
    """
    from agent_eval.adapters import http_adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "DEFAULT_TIMEOUT", httpx.Timeout(0.2))
    server, base = _slow_sse_server(delay=0.6)
    adapter = HttpAgentAdapter(base)
    try:
        session = await adapter.create_session(SessionContext(eval_run_id="r", case_id="c"))
        events = [e async for e in adapter.run(session, AgentRequest(message="hi"))]
    finally:
        await adapter.aclose()
        server.shutdown()
        server.server_close()
    assert [e.type for e in events] == ["run.started", "run.finished"]


async def test_create_session_keeps_a_finite_read_timeout() -> None:
    """反过来：非流式请求**不得**继承"读侧无上限"——它会静默挂死。

    读侧不设上限只对逐轮流式调用成立（那条路径上有 case 级 asyncio.timeout 兜底）。
    create_session 没有别的兜底，挂死就是整次 run 挂死。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions.get("timeout") or {})
        return httpx.Response(200, json={"session_id": "s1"})

    adapter = HttpAgentAdapter("http://mock", client=_mock_client(handler))
    await adapter.create_session(SessionContext(eval_run_id="r", case_id="c"))
    assert seen.get("read") is not None, seen
