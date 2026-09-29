"""本地 mock agent：线程化 HTTP + SSE server（PRD §7/§8 协议），供真实 HTTP 链路联调。

行为由消息内标记驱动（确定性）：脚本与进程内 ``FakeAgentAdapter`` 共用
``adapters/fake.py`` 的 SECURITY_RULES / turn_events——安全用例（PRD §62/§63）
必须能在真实 TCP + SSE 链路上复现，两份脚本各写一份会悄悄漂移。

额外标记（仅本 server）：
  - "[slow]"   → 每轮 sleep 120s（触发 eval 侧超时）

用法::

    uv run python -m agent_eval.dev.mock_server --port 8802
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_eval.adapters.fake import select_script, turn_events

SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "close",
}


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def build_turn_events(message: str) -> list[dict]:
    """Deterministic event script for one run call (与 FakeAgentAdapter 同源)。"""
    script = select_script(message)
    return [
        event.model_dump(mode="json")
        for event in turn_events(message, script, f"trace_{uuid.uuid4().hex[:12]}")
    ]


class MockAgentHandler(BaseHTTPRequestHandler):
    sessions: dict[str, dict] = {}

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        pass

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/agent/sessions":
            session_id = f"sess_{uuid.uuid4().hex[:12]}"
            self.sessions[session_id] = body.get("metadata", {})
            # A1 修订四：workdir 可达性回执（响应侧字段）。mock server 与 harness
            # 同机共享文件系统，恒可达——真实 SUT 必须自行探测后回执。
            self._json(200, {"session_id": session_id, "workdir_accessible": True})
            return
        if self.path.endswith("/run"):
            session_id = self.path.split("/")[-2]
            message = str(body.get("message", ""))
            if "[slow]" in message:
                import time

                time.sleep(120)
            self.send_response(200)
            for key, value in SSE_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True  # SSE 结束即断开，客户端按 EOF 收尾
            for event in build_turn_events(message):
                try:
                    self.wfile.write(_sse(event))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            return
        if self.path.endswith("/cancel"):
            self._json(200, {"cancelled": True})
            return
        self._json(404, {"error": "not found"})


def serve(host: str = "127.0.0.1", port: int = 8802) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), MockAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-eval mock agent (HTTP + SSE)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8802)
    args = parser.parse_args()
    server = serve(args.host, args.port)
    print(f"mock agent listening on http://{args.host}:{args.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
