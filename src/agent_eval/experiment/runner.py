"""Experiment 执行与比较（PRD §22–§28，§106 验收）。

执行语义：
  - 每个 variant 一次 Run，全部 variant 共享同一 benchmark / dataset_version /
    profile / repeat / gate（PRD §110-6：可比性的前提）
  - variant 的 dimension 通过 endpoint 模板（``--agent`` 支持 ``{model}`` 占位）
    与 Run Metadata 落账，实验对比结果因此可回溯到具体配置
  - 任一 variant 出现 exit 2（基础设施失败）时实验状态置 partial，但不丢弃其余 variant
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_eval.experiment.models import Experiment, ExperimentStatus, Variant
from agent_eval.experiment.store import ExperimentStore
from agent_eval.reports.cost import PricingTable
from agent_eval.runner.runner import RunConfig, Runner, RunOutcome
from agent_eval.storage.run_store import RunStore


@dataclass
class VariantResult:
    variant_id: str
    name: str
    dimensions: dict[str, Any]
    run_id: str | None = None
    status: str = "failed"
    verdict: str = "-"
    exit_code: int = 3
    metrics: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    cost: float | None = None
    avg_latency_ms: float = 0.0
    avg_tokens: float = 0.0
    error: str | None = None


@dataclass
class ExperimentRunOutcome:
    experiment: Experiment
    results: list[VariantResult]

    @property
    def exit_code(self) -> int:
        if any(result.exit_code == 2 for result in self.results):
            return 2
        if any(result.exit_code == 1 for result in self.results):
            return 1
        if any(result.exit_code == 3 for result in self.results):
            return 3
        return 0


def render_endpoint(template: str, variant: Variant) -> str:
    """把 variant 维度代入 endpoint 模板（``fake://{model}`` → ``fake://model-a``）。

    未使用的占位符保持原样：模板写错时 endpoint 校验会立刻以 exit 3 报错，
    比静默替换成空串更早暴露问题。
    """
    endpoint = template
    for key, value in variant.dimensions.items():
        endpoint = endpoint.replace("{" + key + "}", str(value))
    return endpoint


def run_experiment(
    experiment: Experiment,
    *,
    evals_root: Path,
    fixtures_root: Path,
    data_root: Path,
    agent_template: str,
    store: ExperimentStore,
) -> ExperimentRunOutcome:
    experiment.status = ExperimentStatus.running
    store.save(experiment)
    pricing = PricingTable.load(evals_root)
    results: list[VariantResult] = []

    for variant in experiment.variants:
        endpoint = variant.agent_endpoint or render_endpoint(agent_template, variant)
        cfg = RunConfig(
            evals_root=evals_root,
            fixtures_root=fixtures_root,
            data_root=data_root,
            benchmark=experiment.benchmark_id,
            agent_endpoint=endpoint,
            profile=experiment.profile,
            repeat=experiment.repeat,
            concurrency=experiment.concurrency,
            gate=variant.gate or experiment.gate,
            baseline_policy=experiment.baseline_policy,
            baseline_run_id=experiment.baseline_run_id,
            experiment_id=experiment.id,
            variant_id=variant.id,
            agent_model=str(variant.dimensions.get("model") or "") or None,
        )
        try:
            outcome: RunOutcome = _run_sync(cfg)
        except Exception as exc:  # noqa: BLE001 — 单 variant 失败不丢弃其余结果
            variant.status = "failed"
            store.set_variant_run(experiment.id, variant.id, "", "failed")
            results.append(
                VariantResult(
                    variant_id=variant.id,
                    name=variant.name,
                    dimensions=variant.dimensions,
                    status="failed",
                    error=str(exc),
                )
            )
            continue
        variant.status = outcome.status
        store.set_variant_run(experiment.id, variant.id, outcome.run_id, outcome.status)
        results.append(
            VariantResult(
                variant_id=variant.id,
                name=variant.name,
                dimensions=variant.dimensions,
                run_id=outcome.run_id,
                status=outcome.status,
                verdict=outcome.verdict,
                exit_code=outcome.exit_code,
                metrics=_run_metrics(data_root, outcome.run_id),
                counts=outcome.aggregate.counts if outcome.aggregate else {},
                cost=outcome.aggregate.cost.total_cost if outcome.aggregate else None,
                avg_latency_ms=(
                    outcome.report.get("totals", {}).get("avg_latency_ms", 0.0)
                    if outcome.report
                    else 0.0
                ),
                avg_tokens=_avg_tokens(outcome),
            )
        )

    status = (
        ExperimentStatus.completed
        if all(result.exit_code in {0, 1} for result in results)
        else ExperimentStatus.partial
    )
    if all(result.error for result in results) and results:
        status = ExperimentStatus.failed
    store.finish(experiment, status)
    _ = pricing  # 定价表已在各 Run 内生效；此处保留入口供后续实验级成本预估
    return ExperimentRunOutcome(experiment=experiment, results=results)


def _run_sync(cfg: RunConfig) -> RunOutcome:
    import asyncio

    return asyncio.run(Runner(cfg).run())


def _run_metrics(data_root: Path, run_id: str) -> dict[str, float]:
    store = RunStore(data_root / "runs")
    try:
        meta, results = store.load_run(run_id)
    except Exception:  # noqa: BLE001 — 投影失败不影响实验表
        return {}
    from agent_eval.reports.aggregate import run_metrics

    return run_metrics(results)


def _avg_tokens(outcome: RunOutcome) -> float:
    totals = (outcome.report or {}).get("totals", {})
    iterations = totals.get("iterations") or 1
    return round((totals.get("total_tokens") or 0) / iterations, 6)


def variant_matrix(
    results: list[VariantResult], dimension: str | None = None
) -> dict[str, list[VariantResult]]:
    """PRD §26/§27/§28：按维度分组（model / prompt / harness 对比视图）。"""
    if dimension is None:
        return {"all": list(results)}
    groups: dict[str, list[VariantResult]] = {}
    for result in results:
        groups.setdefault(str(result.dimensions.get(dimension, "")), []).append(result)
    return groups
