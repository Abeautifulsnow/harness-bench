"""API 运行上下文：把"读哪些目录、拿哪些 store"收敛到一个对象。

只读约束：本模块只实例化各 store 的读取方法，不调用任何写入路径。
派生层（DuckDB）以**只读模式**按需打开，投影不存在时返回 None，由路由降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_eval.experiment.store import ExperimentStore
from agent_eval.failures.promote import DraftStore
from agent_eval.review.store import ReviewStore
from agent_eval.storage.analytics import Analytics, open_readonly
from agent_eval.storage.baseline_store import BaselineStore
from agent_eval.storage.run_store import RunStore


@dataclass
class Workspace:
    """One served project root: definition tree + run facts + derived state."""

    evals_root: Path
    data_root: Path

    @property
    def runs_root(self) -> Path:
        return self.data_root / "runs"

    @property
    def state_root(self) -> Path:
        return self.data_root / "state"

    @property
    def analytics_path(self) -> Path:
        return self.data_root / "analytics.duckdb"

    def run_store(self) -> RunStore:
        return RunStore(self.runs_root)

    def baseline_store(self) -> BaselineStore:
        return BaselineStore(self.state_root, self.runs_root)

    def experiment_store(self) -> ExperimentStore:
        return ExperimentStore(self.state_root)

    def review_store(self) -> ReviewStore:
        return ReviewStore(self.state_root)

    def draft_store(self) -> DraftStore:
        return DraftStore(self.state_root)

    def analytics(self) -> Analytics | None:
        """只读打开投影层；未 rebuild 过时返回 None（调用方转成 missing 提示）。"""
        return open_readonly(self.analytics_path)
