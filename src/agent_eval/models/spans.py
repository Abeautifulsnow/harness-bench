"""Trace Span model (PRD §10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SpanType = Literal[
    "agent",
    "llm",
    "tool",
    "retriever",
    "mcp",
    "skill",
    "subagent",
    "memory",
    "command",
    "workflow",
]


class TraceSpan(BaseModel):
    id: str
    trace_id: str
    parent_span_id: str | None = None

    type: SpanType
    name: str

    started_at: datetime
    finished_at: datetime | None = None

    input: Any = None
    output: Any = None

    attributes: dict[str, Any] = Field(default_factory=dict)

    status: Literal["ok", "error"] = "ok"
    error: str | None = None


class SpanTree:
    """In-memory span tree built from one trace; input to evaluators / DeepEval convert()."""

    def __init__(self, trace_id: str, spans: list[TraceSpan]) -> None:
        self.trace_id = trace_id
        self.spans = spans

    def children(self, parent_id: str | None) -> list[TraceSpan]:
        return [s for s in self.spans if s.parent_span_id == parent_id]

    def find(self, span_type: str, name: str | None = None) -> list[TraceSpan]:
        return [s for s in self.spans if s.type == span_type and (name is None or s.name == name)]

    def tool_sequence(self) -> list[str]:
        """Tool names in call order (used by tool assertions and stability variance)."""
        return [s.name for s in self.spans if s.type == "tool"]

    def token_count(self) -> int:
        total = 0
        for s in self.spans:
            usage = s.attributes.get("usage") or {}
            total += int(usage.get("input_tokens") or 0)
            total += int(usage.get("output_tokens") or 0)
        return total

    def usage_totals(self) -> dict[str, int]:
        """PRD §59 的成本输入：分项累计 input / output / cache tokens。"""
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0}
        for s in self.spans:
            usage = s.attributes.get("usage") or {}
            for key in totals:
                totals[key] += int(usage.get(key) or 0)
        return totals

    def usage_observed(self) -> dict[str, bool]:
        """哪些用量**分量**在本次 trace 里被观测到过（至少一个 span 带了该键）。

        A3 的观测标志来源：``usage_totals`` 里的 0 分不出"真的用了 0"与
        "协议根本没给"——后者才是 `max_tokens` 判 skipped 的依据。
        """
        observed = {"input_tokens": False, "output_tokens": False, "cache_tokens": False}
        for s in self.spans:
            usage = s.attributes.get("usage") or {}
            for key in observed:
                if usage.get(key) is not None:
                    observed[key] = True
        return observed

    def model_names(self) -> list[str]:
        return sorted(
            {str(s.attributes.get("model")) for s in self.spans if s.attributes.get("model")}
        )
