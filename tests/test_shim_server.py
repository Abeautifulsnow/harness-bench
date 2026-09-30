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
import re
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


def _resume_decision(body: dict | None) -> str:
    """从续跑消息的 approval.approved 读决定（假上游复刻被测平台的 upsert 判据）。"""
    message = (body or {}).get("message") or {}
    for part in message.get("parts") or []:
        approval = part.get("approval") or {}
        if "approved" in approval:
            return "approve" if approval["approved"] else "deny"
    return "approve"


def _approval_chunks() -> list[dict]:
    """第一轮：触发审批 → 挂起。形状取自实测 dump（tmp/smoke/smoke-approval.json）。"""
    return [
        {"type": "start", "messageId": "msg-a1"},
        {"type": "start-step"},
        {"type": "text-start", "id": "txt-0"},
        {"type": "text-delta", "id": "txt-0", "delta": "我先问一下。"},
        {
            "type": "tool-input-available",
            "toolCallId": "call_a1",
            "toolName": "ask_user_question",
            "input": {"questions": [{"question": "要什么格式？", "options": ["md", "docx"]}]},
        },
        {"type": "tool-approval-request", "approvalId": "aitxt-a1", "toolCallId": "call_a1"},
        {"type": "text-end", "id": "txt-0"},
        {"type": "finish-step"},
        {"type": "finish", "finishReason": "tool-calls"},
    ]


def _resume_chunks(decision: str) -> list[dict]:
    """第二轮（续跑）：批准走 tool-output-available；拒绝走 tool-output-denied。

    deny 的形状是实测的：``{"type":"tool-output-denied","toolCallId":"…"}``——只有 id，
    没有 output、没有原因（原因只回落在请求侧的 approval.reason）。
    """
    first: dict = (
        {"type": "tool-output-denied", "toolCallId": "call_a1"}
        if decision == "deny"
        else {
            "type": "tool-output-available",
            "toolCallId": "call_a1",
            "output": {"answers": {}, "timestamp": 1},
        }
    )
    return [
        {"type": "start", "messageId": "msg-a2"},
        first,
        {"type": "start-step"},
        {"type": "text-start", "id": "txt-1"},
        {"type": "text-delta", "id": "txt-1", "delta": "懂了。"},
        {"type": "text-end", "id": "txt-1"},
        {"type": "finish-step"},
        {"type": "finish", "finishReason": "stop"},
    ]


@pytest.fixture
def upstream() -> Iterator[dict]:
    """假上游：/api/license/state 放行；/api/chat 按 ``mode`` 决定怎么答。"""
    state: dict = {"mode": "normal", "chat_calls": 0, "got_body": None, "bodies": []}

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
            state["bodies"].append(state["got_body"])
            chunks = _basic_chunks()
            if mode == "truncated":
                chunks = [c for c in chunks if c["type"] != "finish"]
            elif mode == "approval":
                # 第一轮挂起（审批）；第二轮**从续跑消息里读决定**——这正是被测平台
                # 的判据（服务端按 approval.approved upsert），实测拒绝回
                # tool-output-denied、批准回 tool-output-available。
                chunks = (
                    _approval_chunks()
                    if state["chat_calls"] == 1
                    else _resume_chunks(_resume_decision(state["got_body"]))
                )
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


# 4. 终局事件三形态（PRD §6.2.1 义务 5）：不假绿纪律在 shim 侧的落点。
async def test_normal_turn_reaches_a_documented_terminal_shape(
    shim: str, upstream: dict
) -> None:
    """上游正常：事件链以 ``run.finished(status=success)`` 收尾，且**没有** error。

    三种终局形态是互斥的（5xx+原因 / 流内 error+run.finished / 正常 finish），
    任何一个都不能缺席。这一条钉住最容易被"补终局事件"的改动破坏的那一侧：
    正常路径不许被顺手加上兜底的 error。
    """
    adapter, _, _, events = await _turn(shim)
    try:
        types = [e.type for e in events]
        assert types[0] == "run.started"
        assert types[-1] == "run.finished"
        assert events[-1].data["status"] == "success"
        assert "error" not in types
        assert events[-1].data["output"] == "QUERY COMPLETE"
    finally:
        await adapter.aclose()


async def test_terminal_event_failure_keeps_evidence_emitted_before_it(
    shim: str, upstream: dict
) -> None:
    """首事件之后的失败：流内补终局事件时，**已经观测到的证据必须留在流里**。

    这是"超时会掩盖它自己的现场"（ROADMAP 发现的 6）在接入侧的对应物：补终局事件
    的正解是追加，不是把这一轮重写成"什么都没发生"。若 shim 选择丢弃已发出的
    chunk，harness 的 token / 工具调用会一起归零，SUT 到底跑到哪一步就不可考了。
    """
    upstream["mode"] = "truncated"
    adapter, _, _, events = await _turn(shim)
    try:
        types = [e.type for e in events]
        assert "model.response" in types, "截断前已观测到的 token 证据不能丢"
        assert events[-1].type == "run.finished"
        assert events[-1].data["status"] == "error"
    finally:
        await adapter.aclose()


async def test_settings_have_a_default_for_every_key_the_source_touches() -> None:
    """源码里出现的每个 SETTINGS 键都必须在**模块级默认值**里存在。

    不写死键名清单——从 server.py 源码扫出所有 ``SETTINGS["..."]`` 访问。写死清单
    只挡得住已知的那个键，而这一类缺陷的形态恰恰是"新增了一个读点、忘了写点"：
    ``--timeout`` 曾是只有读点没有写点的那个键，读点在请求处理中间、模块级又没有
    默认值，于是每次 /run 都在头已发出之后抛 KeyError（联调实测的 200+空流）。

    判据取"导入时的 SETTINGS"：CLI 的写点在 ``main()`` 里，只有真的从命令行启动
    才执行——测试与嵌入用法都拿不到它，所以默认值必须是模块级的。
    """
    source = (REPO / "shims" / "ai-chatbot" / "shim_ai_chatbot" / "server.py").read_text(
        encoding="utf-8"
    )
    touched = set(re.findall(r'''SETTINGS\["([a-z_]+)"\]''', source))
    assert len(touched) >= 4, f"扫描逻辑失效：只扫出 {sorted(touched)}"
    for key in sorted(touched):
        assert key in shim_server.SETTINGS, (
            f"SETTINGS[{key!r}] 在源码里被访问但没有模块级默认值"
            "——联调实测的 200+空流就是这么来的"
        )


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


# 5. 审批续跑闭环（change-plan §7 第五轮未覆盖清单第 1 项）。
#    这一层是 server.py 的 while 循环 + suspended 检查 + 续跑拼接——转译器单测
#    覆盖不到，而联调首轮的教训正是"两个进程拼起来"的那一层会整体失守。
async def test_approval_round_trip_is_one_stream_with_one_terminal_event(
    shim: str, upstream: dict
) -> None:
    """挂起 → 自动批准 → 续跑 → 一条 §8 流收尾。

    钉住四件事：
      1. 上游被 POST **两次**（挂起不是终局，必须真去续跑）；
      2. §8 流里只有一个 ``run.started``（第二条上游流的 start 不得重复上报）；
      3. 只有一个 ``run.finished``，且为 success、没有 error；
      4. 续跑消息的形状 = 末条 assistant + ``approval-responded`` + ``approved: true``
         —— 这是被测平台 upsert 的唯一判据（实测：approve 不带 reason 也被接受）。
    """
    upstream["mode"] = "approval"
    adapter, _, session, events = await _turn(shim)
    try:
        types = [e.type for e in events]
        assert upstream["chat_calls"] == 2, "审批挂起后必须真的续跑（不是就地收尾）"
        assert types.count("run.started") == 1
        assert types.count("run.finished") == 1
        assert "error" not in types
        assert events[-1].data["status"] == "success"
        # 跨两次上游 POST 的 span 配对：tool.result 指回第一条流的 tool.call
        call = next(e for e in events if e.type == "tool.call")
        result = next(e for e in events if e.type == "tool.result")
        assert result.parent_span_id == call.event_id
        assert call.data["name"] == "ask_user_question"

        first_body, resume_body = upstream["bodies"]
        assert first_body["conversationId"] == resume_body["conversationId"]
        assert first_body["message"] == "ask"
        message = resume_body["message"]
        assert message["role"] == "assistant"
        assert message["id"] == "msg-a1"  # start chunk 的 messageId
        part = next(p for p in message["parts"] if p.get("state") == "approval-responded")
        assert part["approval"]["approved"] is True
        assert part["toolCallId"] == "call_a1"
    finally:
        await adapter.aclose()


async def test_auto_deny_resume_is_reported_as_denied_not_as_a_failure(
    shim: str, upstream: dict
) -> None:
    """拒绝路径：请求侧带 approved=false + reason，流上回 tool-output-denied。

    语义必须落在 ``denied`` 而不是 ``error``：工具没有失败，是策略拒绝了执行，agent
    随后的降级作答是正常路径。原先没有这个处理函数 → 按方言丢弃 → span 永不闭合，
    builder 补成 "span never closed" —— 一次"用户拒绝"被报告成"工具调用失败"。
    """
    upstream["mode"] = "approval"
    shim_server.SETTINGS["policy"] = "auto-deny"  # 策略是 SETTINGS 的事实源（B3 可追溯）
    adapter, _, _, events = await _turn(shim)
    try:
        types = [e.type for e in events]
        assert upstream["chat_calls"] == 2
        assert types.count("run.finished") == 1
        assert "error" not in types
        assert events[-1].data["status"] == "success"
        result = next(e for e in events if e.type == "tool.result")
        assert result.data["status"] == "denied"

        resume_body = upstream["bodies"][1]
        part = next(
            p
            for p in resume_body["message"]["parts"]
            if p.get("state") == "approval-responded"
        )
        assert part["approval"]["approved"] is False
        # reason 是拒绝原因的唯一落点：流上回来的 tool-output-denied 只有 toolCallId
        assert part["approval"]["reason"]
    finally:
        await adapter.aclose()
