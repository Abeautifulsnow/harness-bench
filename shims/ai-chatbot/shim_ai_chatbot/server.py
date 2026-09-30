"""ai-chatbot → harness-bench 协议转译 shim：四端点 HTTP 服务（纯标准库实现）。

对上实现 harness-bench 的接入契约（PRD §7 + change-plan B1）：
  GET  /health                          → 可达性 + license + 观测面能力表 + 实际模型
  POST /api/agent/sessions              → 分配会话，回执 workdir 可达性
  POST /api/agent/sessions/{id}/run     → UIMessage 流转译为 PRD §8 SSE（含审批续跑拼接）
  POST /api/agent/sessions/{id}/cancel  → 调被测平台 stop 端点（best-effort）

对下调 ai-chatbot 原生 API（POST /api/chat，UIMessage stream）。
并发闸默认 4（< 被测平台 CHAT_MAX_CONCURRENCY=5，B4）。

运行：uv run python shims/ai-chatbot/shim_ai_chatbot/server.py --port 8901 \
        --upstream http://localhost:3000 [--model <name>] [--policy auto-approve]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import threading
import uuid
from http import HTTPStatus
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from shim_ai_chatbot.translator import TranslationSession

CONCURRENCY = 4  # B4：必须压在被测平台 CHAT_MAX_CONCURRENCY=5 之下
SESSIONS: dict[str, dict] = {}
_SEM = threading.BoundedSemaphore(CONCURRENCY)
_LOCK = threading.Lock()

SETTINGS: dict = {
    "upstream": "http://localhost:3000",
    "model": None,  # 显式模型（A4：不依赖平台默认值）；None = 平台默认
    "policy": "auto-approve",  # 审批策略（B3：必须可追溯——本声明即策略的事实源）
    "allow_public_upstream": False,
}


# ------------------------------------------------------------------ upstream


def _upstream_candidates(allow_public: bool) -> list[tuple[str, str]]:
    """解析上游并把**全部**合法地址作为连接候选（getaddrinfo 顺序）。

    返回 [(connect_host, host_header)]。Windows 上 `localhost` 通常先解析出 IPv6
    `::1`，而 `next dev -H 0.0.0.0` 只绑 IPv4——只钉第一个地址会钉错族（实测踩坑：
    WinError 10061），所以逐个候选尝试。https 上游不做 IP 钉定（TLS SNI/证书校验
    按主机名走），主机名边界仍由本函数校验。
    """
    split = urlsplit(SETTINGS["upstream"])
    if split.scheme not in ("http", "https"):
        raise SystemExit(f"拒绝：上游协议必须是 http/https，得到 {split.scheme!r}")
    host = split.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SystemExit(f"拒绝：无法解析上游主机 {host!r}: {exc}") from exc
    candidates: list[tuple[str, str]] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local) and not allow_public:
            raise SystemExit(f"拒绝：上游 {host} 解析到公网地址 {ip}（确需请加 --allow-public）")
        connect_host = f"[{ip}]" if ":" in str(ip) else str(ip)
        candidate = (connect_host, host) if split.scheme == "http" else (host, host)
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise SystemExit(f"拒绝：上游 {host} 无可用解析地址")
    return candidates


def _connect(method: str, path: str, body: dict | None, timeout: float) -> HTTPConnection:
    split = urlsplit(SETTINGS["upstream"])
    candidates = _upstream_candidates(SETTINGS["allow_public_upstream"])
    port = split.port or (443 if split.scheme == "https" else 80)
    conn_cls = HTTPSConnection if split.scheme == "https" else HTTPConnection
    headers = {"Accept": "text/event-stream"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    target = f"{split.path.rstrip('/')}{path}" if split.path not in ("", "/") else path
    last_exc: OSError | None = None
    for connect_host, host_header in candidates:
        conn = conn_cls(connect_host, port, timeout=timeout)
        try:
            # Host 头保留原主机名（路由/虚拟主机语义不变）
            conn.request(method, target, body=payload, headers={**headers, "Host": host_header})
            return conn
        except OSError as exc:  # 连接被拒/超时：换下一个解析地址（IPv4/IPv6 双栈兼容）
            last_exc = exc
            conn.close()
    raise OSError(
        f"upstream {SETTINGS['upstream']} unreachable（候选 {candidates}，端口 {port}）：{last_exc}"
    )


# ------------------------------------------------------------------ handlers


def _health_payload() -> tuple[int, dict]:
    """/health：可达性 + license（/api/license/state 在豁免前缀内）+ 观测面 + 模型。"""
    try:
        conn = _connect("GET", "/api/license/state", None, timeout=10.0)
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8") or "{}")
        conn.close()
    except OSError as exc:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "status": "unreachable",
            "detail": f"upstream unreachable: {exc}",
        }
    if not body.get("isValid"):
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "status": "license-invalid",
            "detail": f"upstream license state: {body}",
        }
    return HTTPStatus.OK, {
        "status": "ok",
        # E2 观测面能力表（事件名 → bool）：实测事实（change-plan B2/A2）——
        # provider 重试只写日志不上协议流、压缩只有预算数字无起止事件。
        "observation_surface": {
            "run.started": True,
            "run.finished": True,
            "tool.call": True,
            "tool.result": True,
            "mcp.call": True,
            "mcp.result": True,
            "command.started": True,
            "command.finished": True,
            "subagent.started": True,
            "subagent.finished": True,
            "skill.loaded": True,
            "retry": False,
            "context.compaction.started": False,
            "context.compaction.finished": False,
        },
        # A4：实际生效模型。shim 配置了显式模型则上报之；否则如实声明平台默认。
        "agent_model": SETTINGS["model"] or "platform-default",
        "approval_policy": SETTINGS["policy"],  # B3：策略可追溯
        "detail": "ai-chatbot shim (change-plan §2)",
    }


def _create_session(payload: dict) -> tuple[int, dict]:
    metadata = payload.get("metadata") or {}
    workdir = str(metadata.get("workdir", ""))
    # A1 修订二/四：回执可达性——目录存在且可写才算可达，不得默认可达
    accessible = bool(workdir) and Path(workdir).exists() and Path(workdir).is_dir()
    if accessible:
        probe = Path(workdir) / f".shim-probe-{uuid.uuid4().hex[:8]}"
        try:
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            accessible = False
    session_id = f"sess-{uuid.uuid4().hex[:12]}"
    with _LOCK:
        SESSIONS[session_id] = {
            # conversationId 由调用方自选（实测：缺失 400）——shim 用可辨识前缀
            "conversation_id": f"eval-{uuid.uuid4().hex[:12]}",
            "workdir": workdir,
            "metadata": metadata,
        }
    return HTTPStatus.OK, {"session_id": session_id, "workdir_accessible": accessible}


def _sse(data: dict) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _iter_platform_events(body: dict, timeout: float):
    """POST /api/chat 并逐行 yield 解析后的 UIMessage chunk（SSE data 载荷）。"""
    conn = _connect("POST", "/api/chat", body, timeout=timeout)
    resp = conn.getresponse()
    if resp.status != 200:
        error_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        raise RuntimeError(f"upstream HTTP {resp.status}: {error_body[:300]}")
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                if payload and payload != "[DONE]":
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        raise RuntimeError(
                            f"upstream SSE payload is not JSON: {payload[:200]}"
                        ) from None
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    conn.close()


def _run_stream(session: dict, message: str) -> dict | bytes:
    """一个 turn：POST → 转译 →（审批挂起则续跑）→ 输出 §8 SSE 字节流。

    返回 (status, generator)；generator 逐块产出 SSE bytes。
    """
    translator = TranslationSession(
        trace_id=session["conversation_id"],
        user_message=message,
        model=SETTINGS["model"],
        policy=SETTINGS["policy"],
    )

    def generate():
        chat_body: dict = {
            "conversationId": session["conversation_id"],
            "message": message,
        }
        if session.get("workdir"):
            chat_body["projectDir"] = session["workdir"]  # A1：沙箱路径交给被测方
        if SETTINGS["model"]:
            # A4：显式模型，不依赖平台默认值
            chat_body["modelName"] = SETTINGS["model"]
        rounds = 0
        while True:
            rounds += 1
            last_error: str | None = None
            chunks = None
            try:
                chunks = _iter_platform_events(chat_body, timeout=float(SETTINGS["timeout"]))
                for chunk in chunks:
                    for event in translator.feed(chunk):
                        yield _sse(event)
                    if translator.finished:
                        break
            except RuntimeError as exc:
                last_error = str(exc)
            if translator.finished:
                return
            if last_error is not None:
                # 上游非 200（402 license / 500…）：转成流内 error + run.finished(status=error)。
                # harness 对流内 error 判 AGENT_FAILURE——license 类其实属基础设施，但 v1 的
                # /health 已把 license 状态前置暴露（unhealthy → exit 2），此处兜底保证不假绿。
                yield _sse(
                    translator._event("error", {"message": last_error})  # noqa: SLF001
                )
                yield _sse(
                    translator._event(
                        "run.finished",
                        {"status": "error", "output": ""},
                        parent=translator._root_id,
                    )
                )
                return
            if translator.suspended:
                translator.suspended = False
                resume_message = translator.build_resume_message()
                if resume_message is None:
                    yield _sse(
                        translator._event(
                            "error",
                            {"message": "approval suspended but cannot build resume message"},
                        )
                    )
                    return
                chat_body = {
                    "conversationId": session["conversation_id"],
                    "message": resume_message,
                }
                continue
            return

    return generate()


def _cancel(session: dict) -> tuple[int, dict]:
    try:
        conn = _connect("POST", f"/api/chat/{session['conversation_id']}/stop", {}, timeout=10.0)
        conn.getresponse().read()
        conn.close()
    except OSError:
        return HTTPStatus.OK, {"cancelled": False}  # best-effort：结果已定，不影响结论
    return HTTPStatus.OK, {"cancelled": True}


# ------------------------------------------------------------------ http glue


def build_handler_class():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = "shim-ai-chatbot/0.1"

        def log_message(self, fmt: str, *args: object) -> None:  # quiet
            pass

        def _json(self, code: int, body: dict) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _session(self) -> dict | None:
            sid = self.path.split("/")[-2] if self.path.endswith(("/run", "/cancel")) else ""
            return SESSIONS.get(sid)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                code, body = _health_payload()
                self._json(code, body)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            if self.path == "/api/agent/sessions":
                code, body = _create_session(request)
                self._json(code, body)
                return
            if self.path.endswith("/run"):
                session = self._session()
                if session is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "unknown session"})
                    return
                message = str(request.get("message", ""))
                with _SEM:  # B4：并发压在被测平台容量之下
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        for piece in _run_stream(session, message):
                            self.wfile.write(piece)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                return
            if self.path.endswith("/cancel"):
                session = self._session()
                if session is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "unknown session"})
                    return
                code, body = _cancel(session)
                self._json(code, body)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler, ThreadingHTTPServer


def serve(host: str = "127.0.0.1", port: int = 8901):
    handler_cls, server_cls = build_handler_class()
    server = server_cls((host, port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="ai-chatbot → harness-bench 协议转译 shim")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--upstream", default=SETTINGS["upstream"], help="被测平台基址")
    parser.add_argument("--model", default=None, help="显式模型名（A4；缺省=平台默认）")
    parser.add_argument(
        "--policy", default="auto-approve", choices=["auto-approve"], help="审批策略（B3 可追溯）"
    )
    parser.add_argument(
        "--allow-public-upstream", action="store_true", help="允许公网上游（默认仅回环/私网）"
    )
    args = parser.parse_args()
    SETTINGS["upstream"] = args.upstream
    SETTINGS["model"] = args.model
    SETTINGS["policy"] = args.policy
    SETTINGS["allow_public_upstream"] = args.allow_public_upstream
    candidates = _upstream_candidates(args.allow_public_upstream)
    server = serve(args.host, args.port)
    print(f"shim-ai-chatbot listening on http://{args.host}:{args.port} → {args.upstream}")
    print(f"upstream candidates: {candidates}")
    print(f"approval policy: {args.policy}; model: {args.model or 'platform-default'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
