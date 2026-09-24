"""Experiment 存储与执行（PRD §22–§28，§106 验收）。

存储布局（与 baselines 同构：作者态 append-only，投影进 DuckDB）::

    <data_root>/state/experiments/<experiment_id>.json   定义 + variant 运行态
    <data_root>/runs/<run_id>/                            每个 variant 一个 Run
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.experiment.models import (
    Experiment,
    ExperimentMatrix,
    ExperimentStatus,
    Variant,
)
from agent_eval.ids import new_id


class ExperimentStore:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root / "experiments"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, experiment_id: str) -> Path:
        return self.root / f"{experiment_id}.json"

    def save(self, experiment: Experiment) -> Path:
        path = self.path(experiment.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def load(self, experiment_id: str) -> Experiment:
        path = self.path(experiment_id)
        if not path.is_file():
            raise InvalidCallError(f"experiment not found: {experiment_id}")
        try:
            return Experiment.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise InvalidCallError(f"invalid experiment definition {path.name}: {exc}") from exc

    def list(self) -> list[Experiment]:
        out: list[Experiment] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                out.append(Experiment.model_validate(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:  # noqa: BLE001 — 迁移期脏文件不阻断列表
                continue
        return out

    # -------------------------------------------------------------- authoring

    def create(
        self,
        name: str,
        benchmark_id: str,
        *,
        matrix: ExperimentMatrix | None = None,
        variants: list[dict] | None = None,
        profile: str | None = None,
        repeat: int | None = None,
        concurrency: int = 4,
        gate: str = "pr",
        baseline_run_id: str | None = None,
        baseline_policy: str | None = None,
        dataset_version: str = "",
        note: str = "",
    ) -> Experiment:
        experiment = Experiment(
            id=new_id("exp"),
            name=name,
            benchmark_id=benchmark_id,
            dataset_version=dataset_version,
            profile=profile,
            repeat=repeat,
            concurrency=concurrency,
            gate=gate,
            baseline_run_id=baseline_run_id,
            baseline_policy=baseline_policy,
            note=note,
        )
        combos: list[dict] = []
        if matrix is not None:
            combos = matrix.expand()
        elif variants:
            combos = [dict(v) for v in variants]
        if not combos:
            combos = [{}]  # 单 variant 实验：没有受控维度，仅作基线跑
        for index, dimensions in enumerate(combos, start=1):
            experiment.variants.append(
                Variant(
                    id=new_id("var"),
                    name=_variant_name(dimensions, index),
                    dimensions=dimensions,
                )
            )
        self.save(experiment)
        return experiment

    def set_variant_run(
        self, experiment_id: str, variant_id: str, run_id: str, status: str
    ) -> Experiment:
        experiment = self.load(experiment_id)
        for variant in experiment.variants:
            if variant.id == variant_id:
                variant.run_id = run_id
                variant.status = status
                break
        else:
            raise InvalidCallError(f"variant not found: {variant_id}")
        self.save(experiment)
        return experiment

    def finish(self, experiment: Experiment, status: ExperimentStatus) -> Experiment:
        experiment.status = status
        experiment.finished_at = datetime.now().astimezone()
        self.save(experiment)
        return experiment


def _variant_name(dimensions: dict, index: int) -> str:
    if not dimensions:
        return f"variant-{index}"
    return "-".join(str(dimensions[key]) for key in sorted(dimensions))
