"""Metric Profile (Spec V2.1.1 §7.1 list format; PRD §39 block format is legacy)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MetricSpec(BaseModel):
    id: str  # platform metric id, namespaced: native.* | agent.* | harness.* | custom.*
    provider: str | None = None  # override registry default provider
    threshold: float | None = None
    blocking: bool = False
    fallback: str | None = None  # metric id, dot namespace (Spec §7.1)


class MetricProfile(BaseModel):
    name: str
    metrics: list[MetricSpec] = Field(default_factory=list)
    judge_concurrency: int = 2  # PRD §86: separate from agent concurrency
