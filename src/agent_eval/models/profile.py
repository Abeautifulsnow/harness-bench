"""Metric Profile (Spec V2.1.1 §7.1 list format; PRD §39 block format is legacy)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MetricSpec(BaseModel):
    id: str  # platform metric id, namespaced: native.* | agent.* | harness.* | custom.*
    provider: str | None = None  # override registry default provider
    threshold: float | None = None
    blocking: bool = False
    fallback: str | None = None  # metric id, dot namespace (Spec §7.1)
    # 插件的声明式配置（Spec §17.2）。没有它，插件只能硬编码判定口径，
    # 同一 metric 在不同 case 上无法用不同阈值——而"每个 case 的理想步数/授权集
    # 不同"正是 harness 专项指标存在的理由。
    params: dict[str, Any] = Field(default_factory=dict)


class MetricProfile(BaseModel):
    name: str
    metrics: list[MetricSpec] = Field(default_factory=list)
    judge_concurrency: int = 2  # PRD §86: separate from agent concurrency
    # E1：协议词汇校验的升级开关（未知事件类型 → exit 2）。**独立于 Gate 的
    # strict 字段**——那个字段的语义是"任何 blocking FAIL 都阻断"（Spec §15），
    # 把协议严格性塞进去会让 Release Gate 的行为取决于一个与门禁无关的维度。
    # 收尾档（nightly/strict）开，PR 档保持 warn——PR 上不红是为了不打断，
    # 夜间档红是为了别让"CI 里不红的东西烂掉"。
    strict_protocol: bool = False
