"""shim 服务端端到端测试：真 TCP + 假上游，harness 的 HttpAgentAdapter 直连 shim。

存在理由（联调实测教训）：此前 shim 的测试只覆盖 ``translator.py``——转译规则全绿，
而 ``server.py`` 每次 ``/run`` 都在 ``SETTINGS["timeout"]`` 上抛 KeyError。头已发出，
于是 harness 收到 **200 + 空流**，五个 case 全判 AGENT_FAILURE，被测平台一次都没被
调用。**该假绿在单元测试里完全不可见**：它出在"两个进程拼起来"的那一层。

因此本文件只测拼接面，且都走真实 socket：
  1. 正常一轮 → 事件链完整、usage 单侧口径如实上报；
  2. 上游不可达 → **不为空流**（5xx 带原因 / 或流内终局 error）；
  3. 上游 200 但无 finish chunk → 流内终局 error，不静默截断；
  4. workdir 回执按目录真实可写性给。
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shims" / "ai-chatbot"))

from shim_ai_chatbot import server as shim_server  # noqa: E402

from agent_eval.adapters.base import AgentRequest, SessionContext  # noqa: E402
from agent_eval.adapters.http_adapter import HttpAgentAdapter  # noqa: E402
from agent_eval.errors import InfraError  # noqa: E402


def _serve_shim() -> tuple[ThreadingHTTPServer, str]:
    """起 shim 并取回真实端口。

    绑 `0` 而不是"先探一个空闲端口再绑回"：Windows 上临时端口可能在两次调用之间
    被保留，探测式取端口会以 WinError 10013 失败（实测）。
    """
    httpd = shim_server.serve("127.0.0.1", 0)
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _basic_chunks() -> list[dict]:
    return [
        {"type": "start", "messageId": "msg-1"},
        {"type": "start-step"},
        {"type": "text-start", "id": "txt-0"},
        {"type": "text-delta", "id": "txt-0", "delta": "QUERY COMPLETE"},
        {"type": "text-end", "id": "txt-0"},
        {"type": "finish-step"},
        {
            "type": "data-context-usage",
            "id": "ctx-1",
            "data": {"actualInputTokens": 40921, "cachedTokens": 1152},
        },
        {"type": "finish", "finishReason": "stop"},
    ]


@pytest.fixture
def upstream() -> Iterator[dict]:
    """假上游：/api/license/state 放行；/api/chat 按 ``mode`` 决定怎么答。"""
    state: dict = {"mode": "normal", "chat_calls": 0, "got_body": None}

    class Upstream(BaseHTTPRequestHandler):
        server_version = "fake-ai-chatbot/0"

        def log_message(self, fmt: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"isValid": True}).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            state["chat_calls"] += 1
            state["got_body"] = json.loads(self.rfile.read(length) or b"{}")
            mode = state["mode"]
            if mode == "http-error":
                body = b'{"error":"license required"}'
                self.send_response(HTTPStatus.PAYMENT_REQUIRED)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            chunks = _basic_chunks()
            if mode == "truncated":
                chunks = [c for c in chunks if c["type"] != "finish"]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for chunk in chunks:
                self.wfile.write(_sse(chunk))
                self.wfile.flush()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def shim(upstream: dict) -> Iterator[str]:
    """起 shim（真 TCP），返回其 endpoint。每个用例后关停并复位 SETTINGS。"""
    original = dict(shim_server.SETTINGS)
    shim_server.SETTINGS["upstream"] = upstream["url"]
    shim_server.SETTINGS["allow_public_upstream"] = False
    shim_server.SETTINGS["model"] = None
    shim_server.SETTINGS["policy"] = "auto-approve"
    shim_server.SETTINGS["timeout"] = 30.0
    shim_server.SESSIONS.clear()
    httpd, endpoint = _serve_shim()
    try:
        yield endpoint
    finally:
        httpd.shutdown()
        httpd.server_close()
        shim_server.SESSIONS.clear()
        shim_server.SETTINGS.clear()
        shim_server.SETTINGS.update(original)


async def _turn(endpoint: str, workdir: str | None = None):
    adapter = HttpAgentAdapter(endpoint)
    try:
        health = await adapter.health_check()
        assert health.ok, health.detail
        session = await adapter.create_session(
            SessionContext(
                eval_run_id="r",
                case_id="c",
                iteration=1,
                extra={"workdir": workdir} if workdir else {},
            )
        )
        events = [e async for e in adapter.run(session, AgentRequest(message="ask"))]
        return adapter, health, session, events
    except Exception:
        await adapter.aclose()
        raise


async def test_health_reports_surface_and_model(shim: str, upstream: dict) -> None:
    adapter = HttpAgentAdapter(shim)
    try:
        health = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert health.ok
    assert health.agent_model == "platform-default"  # A4：未配 --model 就如实声明
    assert health.observation_surface["tool.call"] is True
    # provider 重试只写日志不上协议流（change-plan B2 实测事实）——必须声明为不可观测，
    # 否则 harness 的 harness.retry 插件会拿"没看到重试"当"没重试过"。
    assert health.observation_surface["retry"] is False


async def test_normal_turn_translates_end_to_end(shim: str, upstream: dict) -> None:
    adapter, _, _, events = await _turn(shim)
    try:
        types = [e.type for e in events]
        assert types[0] == "run.started"
        assert types[-1] == "run.finished"
        assert events[-1].data["status"] == "success"
        assert events[-1].data["output"] == "QUERY COMPLETE"
        # A3 单侧口径：ai-chatbot 只给输入侧用量 → usage 里没有 output_tokens
        response = next(e for e in events if e.type == "model.response")
        assert response.data["usage"] == {"input_tokens": 40921, "cache_tokens": 1152}
        assert upstream["chat_calls"] == 1
        assert upstream["got_body"]["conversationId"]
    finally:
        await adapter.aclose()


async def test_workdir_receipt_reflects_real_writability(shim: str, tmp_path: Path) -> None:
    adapter, _, session, _ = await _turn(shim, workdir=str(tmp_path))
    try:
        assert session.workdir_accessible is True
        # 不存在的目录 → 回执 false（A1：不得默认可达）
        missing = await adapter.create_session(
            SessionContext(
                eval_run_id="r",
                case_id="c",
                iteration=1,
                extra={"workdir": str(tmp_path / "nope")},
            )
        )
        assert missing.workdir_accessible is False
    finally:
        await adapter.aclose()


async def test_upstream_http_error_never_yields_empty_stream(shim: str, upstream: dict) -> None:
    """上游 402：harness 必须看到**原因**，不能是一条 200 空流。

    这是本文件存在的核心理由——修 KeyError 前的行为正相反（200 + 0 事件）。
    """
    upstream["mode"] = "http-error"
    adapter = HttpAgentAdapter(shim)
    try:
        session = await adapter.create_session(
            SessionContext(eval_run_id="r", case_id="c", iteration=1)
        )
        with pytest.raises(InfraError) as excinfo:
            [e async for e in adapter.run(session, AgentRequest(message="ask"))]
        assert "402" in str(excinfo.value) or "upstream" in str(excinfo.value)
    finally:
        await adapter.aclose()


async def test_upstream_truncated_stream_still_terminates(shim: str, upstream: dict) -> None:
    """上游 200 但无 finish chunk：流内补上 error + run.finished(status=error)。

    静默截断在 harness 侧只表现为"流提前结束"，与真实 agent 崩溃不可分辨——
    SUT 一侧不许制造这种模糊。
    """
    upstream["mode"] = "truncated"
    adapter, _, _, events = await _turn(shim)
    try:
        types = [e.type for e in events]
        assert "error" in types
        assert events[-1].type == "run.finished"
        assert events[-1].data["status"] == "error"
    finally:
        await adapter.aclose()


async def test_missing_timeout_setting_is_not_a_silent_empty_stream(upstream: dict) -> None:
    """回归护栏：`SETTINGS["timeout"]` 缺失时必须**响亮地失败**，不是 200 空流。

    联调实测的原缺陷就是这一处 KeyError 被 http.server 吞进日志栈——头已发出，
    harness 只看到"流为空"。这里直接把该键删掉复现，断言连接层拿到的是错误而不是
    静默的空流（无论它表现为 502 还是流内终局 error，都不能是"0 事件 200 OK"）。
    """
    original = dict(shim_server.SETTINGS)
    shim_server.SETTINGS["upstream"] = upstream["url"]
    shim_server.SETTINGS["timeout"] = 30.0
    shim_server.SESSIONS.clear()
    httpd, endpoint = _serve_shim()
    try:
        del shim_server.SETTINGS["timeout"]  # 只删这一项：复现"读点无写点"
        adapter = HttpAgentAdapter(endpoint)
        try:
            session = await adapter.create_session(
                SessionContext(eval_run_id="r", case_id="c", iteration=1)
            )
            observed: list[str] = []
            try:
                observed = [e.type async for e in adapter.run(session, AgentRequest(message="x"))]
            except InfraError as exc:
                observed = [f"InfraError:{exc}"]
            assert observed, "缺失 timeout 时绝不能是 200 空流（联调实测缺陷的回归护栏）"
            assert any(t.startswith(("InfraError", "error", "run.finished")) for t in observed)
        finally:
            await adapter.aclose()
    finally:
        httpd.shutdown()
        httpd.server_close()
        shim_server.SESSIONS.clear()
        shim_server.SETTINGS.clear()
        shim_server.SETTINGS.update(original)


async def test_settings_have_a_value_for_every_read_key() -> None:
    """每个被读的 SETTINGS 键都必须有默认值：`--timeout` 曾只有读点没有写点。"""
    for key in ("upstream", "model", "policy", "timeout", "allow_public_upstream"):
        assert key in shim_server.SETTINGS, key


def test_cli_exposes_timeout_option() -> None:
    """`--timeout` 必须在 CLI 面存在（只加读点不加写点就是本次缺陷的形状）。"""
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "shim_ai_chatbot", "--help"],
        cwd=str(REPO / "shims" / "ai-chatbot"),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "--timeout" in out.stdout


async def test_invalid_body_gets_a_response_not_a_dropped_connection(shim: str) -> None:
    """请求体不是合法 JSON/UTF-8 时必须回 400，不能直接关连接。

    `json.loads` 对非 UTF-8 字节抛的是 `UnicodeDecodeError`（`ValueError` 的子类，
    但不是 `JSONDecodeError`）；只捕后者就漏了。而 `BaseHTTPRequestHandler` 让
    异常冒出去的结果是**不回任何响应**——对端看到 "Empty reply from server"，
    与 shim 内部崩溃不可分辨（联调实测）。护栏在 `do_POST` 的兜底 try 里。
    """
    import httpx

    base = shim.rstrip("/")
    assert httpx.post(f"{base}/api/agent/sessions", json={}, timeout=10.0).status_code == 200
    bad = httpx.post(
        f"{base}/api/agent/sessions",
        content=b"\xb2\xe2\xca\xd4",  # GBK 字节：不是合法 UTF-8
        headers={"Content-Type": "application/json"},
        timeout=10.0,
    )
    assert bad.status_code == 400
    assert "invalid JSON" in bad.text


async def test_unknown_route_gets_404_not_a_dropped_connection(shim: str) -> None:
    import httpx

    resp = httpx.post(f"{shim}/api/nope", json={}, timeout=10.0)
    assert resp.status_code == 404
