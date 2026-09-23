"""Agent Event Protocol models (PRD §8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Event types emitted by the agent runtime over SSE (PRD §8).
EVENT_TYPES = (
    "run.started",
    "run.finished",
    "agent.started",
    "agent.finished",
    "model.request",
    "model.response",
    "tool.call",
    "tool.result",
    "mcp.call",
    "mcp.result",
    "skill.discovered",
    "skill.loaded",
    "retriever.call",
    "retriever.result",
    "subagent.started",
    "subagent.finished",
    "memory.query",
    "memory.result",
    "context.compaction.started",
    "context.compaction.finished",
    "plan.created",
    "plan.updated",
    "file.read",
    "file.write",
    "command.started",
    "command.finished",
    "error",
    "retry",
    "interrupt",
    "cancel",
)


class TraceEvent(BaseModel):
    """Minimal event structure (PRD §8)."""

    event_id: str
    trace_id: str
    parent_span_id: str | None = None
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    data: dict[str, Any] = Field(default_factory=dict)
