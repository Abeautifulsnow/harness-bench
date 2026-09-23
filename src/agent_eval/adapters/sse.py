"""SSE 流解析（PRD §7.3 推荐返回 SSE Event Stream）。

只关心 ``data:`` 负载（每条负载是一个 PRD §8 事件 JSON）；
``event:``/``id:``/注释行/keepalive 均忽略，多条 data 行按 SSE 规范以 \\n 连接。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agent_eval.errors import InfraError
from agent_eval.models.events import TraceEvent


def parse_sse_payload(payload: str) -> TraceEvent | None:
    """Parse one assembled SSE data payload（framer 已剥离 'data:' 前缀）into a TraceEvent."""
    payload = payload.strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InfraError(f"agent SSE payload is not valid JSON: {exc}") from exc
    try:
        return TraceEvent.model_validate(obj)
    except Exception as exc:
        raise InfraError(f"agent SSE event violates PRD §8 schema: {exc}") from exc


class _SseFramer:
    """Buffers byte stream lines; yields assembled data payloads on event boundaries."""

    def __init__(self) -> None:
        self._buffer = b""
        self._data: list[str] = []

    def feed_line(self, line: str) -> str | None:
        if line == "":
            payload = "\n".join(self._data)
            self._data = []
            return payload if payload else None
        if line.startswith(":"):  # comment / keepalive
            return None
        if line.startswith("data:"):
            self._data.append(line[len("data:") :].lstrip())
        # event:/id:/retry: and unknown fields are irrelevant to the eval protocol
        return None

    async def afeed(self, chunk: bytes) -> AsyncIterator[str]:
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            payload = self.feed_line(raw.rstrip(b"\r").decode("utf-8", errors="replace"))
            if payload is not None:
                yield payload

    def flush(self) -> str | None:
        if not self._buffer.strip():
            return None
        payload = self.feed_line(self._buffer.decode("utf-8", errors="replace").rstrip("\r"))
        self._buffer = b""
        # feed_line("") only fires on explicit blank line; flush trailing data directly
        if not payload and self._data:
            payload = "\n".join(self._data)
            self._data = []
        return payload


async def parse_sse_stream(chunks: AsyncIterator[bytes]) -> AsyncIterator[TraceEvent]:
    """Incremental SSE framing over an async byte stream (buffers across chunk splits)."""
    framer = _SseFramer()
    async for chunk in chunks:
        async for payload in framer.afeed(chunk):
            event = parse_sse_payload(payload)
            if event is not None:
                yield event
    tail = framer.flush()
    if tail is not None:
        event = parse_sse_payload(tail)
        if event is not None:
            yield event
