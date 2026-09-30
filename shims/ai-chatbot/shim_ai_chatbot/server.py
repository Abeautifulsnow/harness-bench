"""ai-chatbot → harness-bench 协议转译 shim：四端点 HTTP 服务（纯标准库实现）。

对上实现 harness-bench 的接入契约（PRD §7 + change-plan B1）：
  GET  /health                          → 可达性 + license + 观测面能力表 + 实际模型
  POST /api/agent/sessions              → 分配会话，回执 workdir 可达性
  POST /api/agent/sessions/{id}/run     → UIMessage 流转译为 PRD §8 SSE（含审批续跑拼接）
  POST /api/agent/sessions/{id}/cancel  → 调被测平台 stop 端点（best-effort）

对下调 ai-chatbot 原生 API（POST /api/chat，UIMessage stream）。
并发闸默认 4（< 被测平台 CHAT_MAX_CONCURRENCY=5，B4）。

运行：uv run python shims/ai-chatbot/shim_ai_chatbot/server.py --port 8901 \
        --upstream http://localhost:3000 [--model <name>] [--policy auto-approve] [--timeout 300]

不假绿纪律（联调实测教训）：shim 是**被测方一侧**，它对 harness 说的每句话都是
被测事实。因此任何一条 /run 流都必须以 §8 的终局事件收场——
  - 首个事件之前失败 → 5xx + JSON 原因（harness 判 InfraError / exit 2，运维看得到）
  - 首个事件之后失败 → 流内 error + run.finished(status=error)（判 agent 失败）
绝不出现"200 + 空流"：那与真实的 agent 失败在 harness 侧不可分辨。
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import socket
import threading
import uuid
from collections.abc import Iterator
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
    # 审批策略（B3：必须可追溯——本声明即策略的事实源，随 /health 上报）。
    # auto-deny 不是"跳过审批"：拒绝同样要续跑 POST 才能让上游接着跑（实测）。
    "policy": "auto-approve",
    # 单轮上游读超时（秒）。**必须有默认值**：早先只有 `--timeout` 的读点而无写点，
    # 每次 /run 都在 `SETTINGS["timeout"]` 上抛 KeyError——头已发出，于是 harness 收到
    # "200 + 空流"，五个 case 全判 AGENT_FAILURE，被测平台一次都没被调用（联调实测）。
    "timeout": 300.0,
    "allow_public_upstream": False,
}

# E2 观测面能力表（事件名 → bool）：**实测事实**（change-plan B2/A2）——
# provider 重试只写日志不上协议流、压缩只有预算数字无起止事件。
#
# 必须是模块级常量而不是 `_health_payload` 里的字面量：定义树侧要按它判"哪些
# metric 的观测面不存在"（C 类 profile 的裁剪依据），而唯一能读到它的方式不该是
# 起一个真上游再发 HTTP（tests/test_chatbot_dataset.py 的护栏直接导入它）。
# 声明与事实必须同源：这里改一个字，护栏与 /health 一起变。
OBSERVATION_SURFACE: dict[str, bool] = {
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
        # E2 观测面能力表：定义在模块级（OBSERVATION_SURFACE），与护栏同源。
        "observation_surface": dict(OBSERVATION_SURFACE),
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


def _run_stream(session: dict, message: str) -> tuple[Iterator[bytes], TranslationSession]:
    """一个 turn：POST → 转译 →（审批挂起则续跑）→ 输出 §8 SSE 字节流。

    返回 ``(generator, translator)``；generator 逐块产出 SSE bytes。translator 一并
    交回，是给调用方的兜底出口——流中途出任何意外都要能从它手里拿到终局事件
    （见 ``TranslationSession.fail``），而不是让连接就这么断在半路。
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
        emitted = False  # 是否已经发出过事件——决定失败时还能不能改状态码
        while True:
            failure: Exception | None = None
            try:
                chunks = _iter_platform_events(chat_body, timeout=float(SETTINGS["timeout"]))
                for chunk in chunks:
                    for event in translator.feed(chunk):
                        emitted = True
                        yield _sse(event)
                    if translator.finished:
                        break
            except (OSError, RuntimeError) as exc:
                # 上游非 200（402 license / 500…）、连接被拒、流中途断开、载荷不是 JSON。
                failure = exc
            if translator.finished:
                return
            if failure is not None:
                if not emitted:
                    # 第一个事件都还没发出去：状态码仍可改，**向上抛**让调用方发 5xx。
                    # license 无效、上游连不上都属基础设施故障——harness 该判 exit 2
                    # （"环境没起来"），而不是把 SUT 判成"任务做失败了"。
                    raise failure
                # 已经发过事件：状态码改不了，只能如实补上流内终局事件。harness 对流内
                # error 判 AGENT_FAILURE——不如 5xx 精确，但绝不假绿（B1 兜底语义）。
                reason = f"upstream failure: {type(failure).__name__}: {failure}"
                for event in translator.fail(reason):
                    yield _sse(event)
                return
            if translator.suspended:
                translator.suspended = False
                resume_message = translator.build_resume_message()
                if resume_message is None:
                    for event in translator.fail(
                        "approval suspended but cannot build resume message"
                    ):
                        yield _sse(event)
                    return
                chat_body = {
                    "conversationId": session["conversation_id"],
                    "message": resume_message,
                }
                continue
            # 上游以 200 收场却没给 finish chunk：同样是协议违约。不静默 return——
            # 一条没有终局事件的流在 harness 侧与"agent 崩了"不可分辨（不假绿）。
            for event in translator.fail("upstream stream ended without a finish chunk"):
                yield _sse(event)
            return

    return generate(), translator


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
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # 对端可能已经不在了：harness 在 case 超时后立刻 cancel，响应写下时
                # 连接已由它这一侧关掉（联调实测 WinError 10053）。控制权不在 shim，
                # 也不影响任何判定——不值得让它变成一条 40 行的异常栈。
                self.wfile.write(payload)

        def _session(self) -> dict | None:
            sid = self.path.split("/")[-2] if self.path.endswith(("/run", "/cancel")) else ""
            return SESSIONS.get(sid)

        def _terminate_stream(self, translator: TranslationSession, exc: Exception) -> None:
            """首事件之后的失败：补上终局事件（best-effort，对端已断则无所谓）。"""
            with contextlib.suppress(
                BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError
            ):
                for event in translator.fail(f"shim failure: {type(exc).__name__}: {exc}"):
                    self.wfile.write(_sse(event))
                    self.wfile.flush()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                code, body = _health_payload()
                self._json(code, body)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            # 兜底：do_POST 抛出的任何异常都会让 BaseHTTPRequestHandler 关连接而不回
            # 响应——对端看到的是"Empty reply from server"。它正是本文件的头号禁令
            # （"不许静默失败"）在**请求侧**的等价物：接入口崩了，被测方却背上嫌疑。
            # 联调实测触发过一次：请求体不是 UTF-8 时 `json.loads` 抛的是
            # `UnicodeDecodeError` 而不是 `JSONDecodeError`，只捕后者就漏了。
            try:
                self._handle_post()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return  # 对端已断，没人收
            except Exception as exc:  # noqa: BLE001
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "shim internal error", "detail": f"{type(exc).__name__}: {exc}"},
                )

        def _handle_post(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
                return
            if not isinstance(request, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
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
                    stream, translator = _run_stream(session, message)
                    # 首个事件之前不承诺 200：此时状态码还改得了，失败应当被 harness
                    # 判成基础设施故障（exit 2）而不是"agent 失败了"。
                    try:
                        first = next(stream)
                    except StopIteration:
                        self._json(
                            HTTPStatus.BAD_GATEWAY,
                            {"error": "shim produced no events", "detail": "上游未产生任何 chunk"},
                        )
                        return
                    except Exception as exc:  # noqa: BLE001 — 任何意外都不许伪装成空流
                        self._json(
                            HTTPStatus.BAD_GATEWAY,
                            {
                                "error": "shim upstream failure",
                                "detail": f"{type(exc).__name__}: {exc}",
                            },
                        )
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        self.wfile.write(first)
                        self.wfile.flush()
                        for piece in stream:
                            self.wfile.write(piece)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        # 对端已断（典型：case 超时后 harness 取消这一轮）。没有收件人，
                        # 终局事件无处可送——这不是"静默截断"，是没人听了。
                        return
                    except Exception as exc:  # noqa: BLE001 — 见下：终局事件必须留下
                        # 首事件之后失败：头已发出，状态码改不了；用流内终局事件如实收场。
                        # 「静默截断」在 harness 侧只表现为"流提前结束"，与真实 agent 失败
                        # 不可分辨——shim 是 SUT 一侧，不许制造这种模糊。
                        self._terminate_stream(translator, exc)
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
        "--policy",
        default="auto-approve",
        choices=["auto-approve", "auto-deny"],
        help="审批策略（B3 可追溯；两条路径都要真实续跑 POST，见 translator）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=SETTINGS["timeout"],
        help="单轮上游读超时（秒），覆盖到下一个 chunk 的等待",
    )
    parser.add_argument(
        "--allow-public-upstream", action="store_true", help="允许公网上游（默认仅回环/私网）"
    )
    args = parser.parse_args()
    SETTINGS["upstream"] = args.upstream
    SETTINGS["model"] = args.model
    SETTINGS["policy"] = args.policy
    SETTINGS["timeout"] = args.timeout
    SETTINGS["allow_public_upstream"] = args.allow_public_upstream
    candidates = _upstream_candidates(args.allow_public_upstream)
    server = serve(args.host, args.port)
    print(f"shim-ai-chatbot listening on http://{args.host}:{args.port} → {args.upstream}")
    print(f"upstream candidates: {candidates}")
    print(
        f"approval policy: {args.policy}; model: {args.model or 'platform-default'}; "
        f"turn timeout: {args.timeout:g}s"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
