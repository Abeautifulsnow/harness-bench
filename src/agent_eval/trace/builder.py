"""TraceBuilder：原始事件流 → Event Log + Span Tree（PRD §9/§10）。

配对规则（opening 事件 → closing 事件）：
  run.started → run.finished(agent 根 span，type=agent)
  model.request → model.response(llm)
  tool.call → tool.result(tool)
  mcp.call → mcp.result(mcp)
  retriever.call → retriever.result(retriever)
  memory.query → memory.result(memory)
  command.started → command.finished(command)
  subagent.started → subagent.finished(subagent 容器)
  context.compaction.started → context.compaction.finished(workflow)

- span id = opening 事件的 event_id；父子关系取事件自带 parent_span_id
- closing 事件带 error → span status=error
- 未配对的 opening 在 run.finished 时统一收口；孤立的 closing 事件忽略
"""

from __future__ import annotations

from typing import Any

from agent_eval.models.events import TraceEvent
from agent_eval.models.spans import SpanTree, TraceSpan

_PAIRS: dict[str, tuple[str, str]] = {
    "model.request": ("model.response", "llm"),
    "tool.call": ("tool.result", "tool"),
    "mcp.call": ("mcp.result", "mcp"),
    "retriever.call": ("retriever.result", "retriever"),
    "memory.query": ("memory.result", "memory"),
    "command.started": ("command.finished", "command"),
    "subagent.started": ("subagent.finished", "subagent"),
    "context.compaction.started": ("context.compaction.finished", "workflow"),
}


class TraceBuilder:
    def __init__(self, trace_id: str | None = None) -> None:
        self._trace_id = trace_id or "trace"
        self._events: list[TraceEvent] = []
        self._open: dict[str, TraceEvent] = {}  # event_id -> opening event
        self._spans: list[TraceSpan] = []
        self._root: TraceSpan | None = None
        self._finished = False

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def feed(self, event: TraceEvent) -> None:
        """Consume one protocol event; order must be arrival order."""
        self._events.append(event)
        if event.type == "run.started":
            if self._root is None:
                self._root = TraceSpan(
                    id=event.event_id,
                    trace_id=event.trace_id,
                    parent_span_id=None,
                    type="agent",
                    name="AgentRun",
                    started_at=event.timestamp,
                    input=event.data.get("message"),
                )
            return
        if event.type == "run.finished":
            self._finalize_open(event)
            if self._root is not None:
                self._root.finished_at = event.timestamp
                self._root.output = event.data.get("output", self._root.output)
                if event.data.get("status") == "error":
                    self._root.status = "error"
            self._finished = True
            return
        if event.type in _PAIRS:
            close_type, span_type = _PAIRS[event.type]
            self._open_span(event, span_type, close_type)
            return
        if event.type in {close for close, _ in _PAIRS.values()}:
            self._close_span(event)
            return
        if event.type == "error":
            span = self._owner_span(event)
            if span is not None:
                span.status = "error"
                span.error = str(event.data.get("message", "agent error"))
            return
        if event.type == "model.response":
            # 未配对的 model.response：usage 仍须计入 owner span，不丢观测数据
            span = self._owner_span(event)
            if span is not None:
                span.attributes["usage"] = event.data.get("usage")
            return
        # 其余事件（skill.discovered/plan.*/file.*/retry/...）保留在 event log，
        # 并附加到最近的活跃 span 属性上，不建 span
        span = self._owner_span(event)
        if span is not None:
            span.attributes.setdefault("events", []).append(event.type)

    def _owner_span(self, event: TraceEvent) -> TraceSpan | None:
        by_parent = [s for s in self._spans if s.id == event.parent_span_id]
        if by_parent:
            return by_parent[0]
        if self._root is not None:
            return self._root
        return None

    def _open_span(self, event: TraceEvent, span_type: str, close_type: str) -> None:
        attributes: dict[str, Any] = {"expect_close": close_type}
        # 工具参数单独留档：`input` 的兜底是整份 event.data（含 name），把它当
        # "实际发送的参数"会让 judge 按伪造参数判 ArgumentCorrectness。
        if "arguments" in event.data:
            attributes["arguments"] = event.data.get("arguments")
        span = TraceSpan(
            id=event.event_id,
            trace_id=event.trace_id,
            parent_span_id=event.parent_span_id,
            type=span_type,  # type: ignore[arg-type]
            name=str(event.data.get("name", event.type.split(".")[0])),
            started_at=event.timestamp,
            input=event.data.get("arguments", event.data),
            attributes=attributes,
        )
        self._spans.append(span)
        self._open[event.event_id] = event

    def _close_span(self, event: TraceEvent) -> None:
        parent_id = event.parent_span_id
        span = next((s for s in self._spans if s.id == parent_id), None)
        if span is None:
            return  # 孤立 closing：忽略（保留在 event log）
        span.finished_at = event.timestamp
        span.output = event.data.get("result", event.data.get("text", span.output))
        span.attributes.pop("expect_close", None)
        if event.data.get("status") == "error":
            span.status = "error"
            span.error = str(event.data.get("error", event.data.get("message", "")))
        span.attributes["usage"] = event.data.get("usage")
        if span.type == "tool":
            span.attributes["tool_status"] = event.data.get("status")
        if span.type == "command":
            # Spec §19.4：exit_code 断言的唯一观测来源是 command.finished 的退出码。
            # 协议未提供时保持 None——由求值方判 skipped，不猜成 0。
            span.attributes["exit_code"] = event.data.get("exit_code")
        self._open.pop(span.id, None)

    def _finalize_open(self, finished: TraceEvent) -> None:
        for span in self._spans:
            if span.finished_at is None:
                span.finished_at = finished.timestamp
                span.status = "error"
                span.error = span.error or "span never closed (stream ended)"

    def feed_all(self, events: list[TraceEvent]) -> None:
        for event in events:
            self.feed(event)

    def build(self) -> SpanTree:
        spans = list(self._spans)
        if self._root is not None:
            spans.insert(0, self._root)
        return SpanTree(self._trace_id, spans)
