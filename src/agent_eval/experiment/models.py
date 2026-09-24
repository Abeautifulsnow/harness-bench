"""Experiment 模型（PRD §22–§25，Spec §1.2）。

Experiment 1—N ExperimentVariant；ExperimentVariant 1—N Run。
Variant 是"一次受控变更"的载体：dimensions 里每个 key 对应 PRD §25 的一个实验维度。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# PRD §25 的受控维度。取值只登记为字符串，平台不解释其语义（由 Agent 侧消费）。
VARIANT_DIMENSIONS = (
    "agent_version",
    "model",
    "prompt",
    "system_prompt",
    "planner_prompt",
    "tool_description",
    "tool_policy",
    "tool_set",
    "skill_set",
    "skill_version",
    "mcp_set",
    "memory_strategy",
    "context_strategy",
    "compression_strategy",
    "reasoning_effort",
    "temperature",
)


class ExperimentStatus(StrEnum):
    created = "created"
    running = "running"
    completed = "completed"
    partial = "partial"
    failed = "failed"


class Variant(BaseModel):
    """PRD §23：一次实验中的一个受控组合。"""

    id: str
    name: str
    dimensions: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    status: str = "created"
    # 运行期参数（由 variant 展开而来，不参与实验语义）
    agent_endpoint: str | None = None
    gate: str = "pr"

    def label(self) -> str:
        if not self.dimensions:
            return self.name
        return " / ".join(f"{k}={v}" for k, v in sorted(self.dimensions.items()))


class ExperimentMatrix(BaseModel):
    """PRD §24：笛卡尔积展开为 variants。"""

    axes: dict[str, list[Any]] = Field(default_factory=dict)
    base: dict[str, Any] = Field(default_factory=dict)

    def expand(self) -> list[dict[str, Any]]:
        """按轴顺序做笛卡尔积；轴顺序取声明顺序（YAML 保序），保证 variant 名稳定。"""
        combos: list[dict[str, Any]] = [dict(self.base)]
        for axis, values in self.axes.items():
            if not values:
                continue
            combos = [{**combo, axis: value} for combo in combos for value in values]
        return combos


class Experiment(BaseModel):
    id: str
    name: str
    benchmark_id: str
    dataset_version: str = ""
    profile: str | None = None
    repeat: int | None = None
    concurrency: int = 4
    gate: str = "pr"
    baseline_run_id: str | None = None
    baseline_policy: str | None = None
    status: ExperimentStatus = ExperimentStatus.created
    note: str = ""
    variants: list[Variant] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    finished_at: datetime | None = None

    def by_dimension(self, dimension: str) -> dict[str, list[Variant]]:
        """PRD §26/§27/§28：按单个维度分组（Model / Prompt / Harness Comparison）。"""
        groups: dict[str, list[Variant]] = {}
        for variant in self.variants:
            key = str(variant.dimensions.get(dimension, ""))
            groups.setdefault(key, []).append(variant)
        return groups
