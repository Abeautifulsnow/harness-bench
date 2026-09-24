"""Cost Analysis（PRD §59）。

成本只在有定价表时计算：拿不到 price 就返回 ``None``，绝不把"未知"记成 0.0 ——
0.0 会在趋势图里表现为"成本降到零"，是比缺失更糟的假信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent_eval.errors import InvalidCallError
from agent_eval.models.results import CaseRunResult

# 定价单位：每 1000 tokens 的价格（货币由部署方决定，平台只做同币种相加）
_TOKENS_PER_UNIT = 1000.0


@dataclass(frozen=True)
class ModelPrice:
    input_per_1k: float = 0.0
    output_per_1k: float = 0.0
    cache_per_1k: float | None = None  # 未声明则按 input 价计


class PricingTable:
    """``evals/pricing.yaml``：``models: {<model>: {input, output, cache}}``。"""

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self.prices = prices or {}

    @classmethod
    def load(cls, root: Path) -> PricingTable:
        path = root / "pricing.yaml"
        if not path.is_file():
            return cls()
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise InvalidCallError(f"pricing file must be a mapping: {path}")
        prices: dict[str, ModelPrice] = {}
        for model, spec in (data.get("models") or {}).items():
            if not isinstance(spec, dict):
                raise InvalidCallError(f"pricing entry for '{model}' must be a mapping: {path}")
            try:
                prices[str(model)] = ModelPrice(
                    input_per_1k=float(spec.get("input", 0.0) or 0.0),
                    output_per_1k=float(spec.get("output", 0.0) or 0.0),
                    cache_per_1k=(float(spec["cache"]) if spec.get("cache") is not None else None),
                )
            except (TypeError, ValueError) as exc:
                raise InvalidCallError(f"invalid pricing for '{model}': {exc}") from exc
        return cls(prices)

    def price_for(self, model: str | None) -> ModelPrice | None:
        if model is None:
            return None
        if model in self.prices:
            return self.prices[model]
        return self.prices.get("*")  # 通配兜底：单一费率部署

    def cost(
        self,
        model: str | None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_tokens: int = 0,
    ) -> float | None:
        price = self.price_for(model)
        if price is None:
            return None
        cache_price = price.cache_per_1k if price.cache_per_1k is not None else price.input_per_1k
        return round(
            (
                input_tokens * price.input_per_1k
                + output_tokens * price.output_per_1k
                + cache_tokens * cache_price
            )
            / _TOKENS_PER_UNIT,
            10,
        )


@dataclass
class CostReport:
    """PRD §59 输出项。"""

    total_cost: float | None = None
    agent_cost: float | None = None
    judge_cost: float | None = None
    tool_cost: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    avg_cost_per_case: float | None = None
    cost_per_success: float | None = None
    judge_cost_ratio: float | None = None
    priced_cases: int = 0
    unpriced_cases: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cost": self.total_cost,
            "agent_cost": self.agent_cost,
            "judge_cost": self.judge_cost,
            "tool_cost": self.tool_cost,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_tokens": self.cache_tokens,
            "avg_cost_per_case": self.avg_cost_per_case,
            "cost_per_success": self.cost_per_success,
            "judge_cost_ratio": self.judge_cost_ratio,
            "priced_cases": self.priced_cases,
            "unpriced_cases": self.unpriced_cases,
        }


def compute_cost_report(results: list[CaseRunResult]) -> CostReport:
    """按 CaseRun 聚合成本（PRD §81：Run 级数字一律是 CaseRun 的聚合）。"""
    report = CostReport()
    if not results:
        return report
    priced = [r for r in results if r.cost_known]
    report.priced_cases = len(priced)
    report.unpriced_cases = len(results) - len(priced)
    report.input_tokens = sum(r.input_tokens for r in results)
    report.output_tokens = sum(r.output_tokens for r in results)
    report.cache_tokens = sum(r.cache_tokens for r in results)
    if not priced:
        return report  # 无定价 → 全部为 None，不伪造 0.0
    total = round(sum(r.cost for r in priced), 10)
    judge_total = round(
        sum(
            float(m.metadata.get("cost") or 0.0)
            for r in priced
            for m in r.all_metric_results
            if m.evaluator != "native"
        ),
        10,
    )
    report.total_cost = total
    report.agent_cost = round(total - judge_total, 10)
    report.judge_cost = judge_total if judge_total else None
    report.avg_cost_per_case = round(total / len(priced), 10)
    successes = sum(1 for r in priced if r.status.value == "PASS")
    report.cost_per_success = round(total / successes, 10) if successes else None
    report.judge_cost_ratio = round(judge_total / total, 6) if total else None
    return report
