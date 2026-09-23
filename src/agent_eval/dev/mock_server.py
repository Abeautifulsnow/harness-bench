"""本地 mock agent：线程化 HTTP + SSE server（PRD §7/§8 协议），供真实 HTTP 链路联调。

行为由消息内标记驱动（确定性）：
  - 默认       → 调用 database_schema + execute_sql，输出含 "QUERY COMPLETE"
  - "[fail]"   → 追加 error 事件（run.finished status=error）
  - "[slow]"   → 每轮 sleep 120s（触发 eval 侧超时）
  - "[subagent]" → 额外产生 subagent.started/finished + 内部工具调用

用法::

    uv run python -m agent_eval.dev.mock_server --port 8802
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "close",
}


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def _evt(trace_id: str, parent: str | None, etype: str, data: dict) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "trace_id": trace_id,
        "parent_span_id": parent,
        "type": etype,
        "timestamp": datetime.now().astimezone().isoformat(),
        "data": data,
    }


def build_turn_events(message: str) -> list[dict]:
    """Deterministic event script for one run call."""
    trace_id = f"trace_{uuid.uuid4().hex[:12]}"
    events: list[dict] = [
        _evt(trace_id, None, "run.started", {"message": message}),
        _evt(trace_id, None, "agent.started", {}),
    ]
    agent_span = events[-1]["event_id"]

    def add_tool(name: str, parent: str, status: str = "ok") -> None:
        call = _evt(trace_id, parent, "tool.call", {"name": name, "arguments": {}})
        events.append(call)
        events.append(_evt(trace_id, call["event_id"], "tool.result", {"status": status}))

    if "[subagent]" in message:
        sub = _evt(trace_id, agent_span, "subagent.started", {"name": "researcher"})
        events.append(sub)
        add_tool("web_search", sub["event_id"])
        events.append(_evt(trace_id, sub["event_id"], "subagent.finished", {"status": "ok"}))
    for tool in ("database_schema", "execute_sql"):
        add_tool(tool, agent_span, "error" if "[toolerror]" in message else "ok")

    output = "QUERY COMPLETE: mock agent result"
    if "30 天" in message:
        output += "（已按最近 30 天过滤）"
    llm_span = _evt(trace_id, agent_span, "model.request", {"model": "mock-model"})
    events.append(llm_span)
    events.append(
        _evt(
            trace_id,
            llm_span["event_id"],
            "model.response",
            {"text": output, "usage": {"input_tokens": 120, "output_tokens": 80}},
        )
    )
    failed = "[fail]" in message
    if failed:
        events.append(_evt(trace_id, agent_span, "error", {"message": "mock scripted failure"}))
    events.append(
        _evt(
            trace_id,
            None,
            "run.finished",
            {"status": "error" if failed else "success", "output": output},
        )
    )
    return events


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
            self._json(200, {"session_id": session_id})
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
