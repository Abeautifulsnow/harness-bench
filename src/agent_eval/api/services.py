"""只读服务层：把"加载 run 事实 → 组装聚合视图"收敛到一处。

设计约束：
  - 聚合视图优先复用已落盘的 report.json（Spec §6.2 产物 = 事实），缺失时才用
    ``build_aggregate`` 现场重算，两者都不写任何文件；
  - Gate 视图优先复用 gate.json，缺失时用同一套 Gate Rules 重放（``source`` 字段
    显式标注来源，避免 UI 把"重放结论"当成"当时判定"）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_eval.errors import InvalidCallError
from agent_eval.failures.analysis import AnalysisResult, analyse_run
from agent_eval.models.regression import GateReport, RegressionComparison
from agent_eval.models.results import CaseRunResult
from agent_eval.models.run import RunMetadata
from agent_eval.quality.gates import evaluate_gate, exit_code_for, load_gate_rules
from agent_eval.regression.compare import compare_runs
from agent_eval.reports.aggregate import RunAggregate, build_aggregate
from agent_eval.storage.run_store import RunStore

ARTIFACT_NAMES = ("report.json", "gate.json", "junit.xml", "report.html", "summary.md")
TEXT_ARTIFACTS = ("report.json", "gate.json", "junit.xml", "summary.md")

CONTENT_TYPES = {
    "report.json": "application/json",
    "gate.json": "application/json",
    "junit.xml": "application/xml",
    "summary.md": "text/markdown",
    "report.html": "text/html",
}


class RunView:
    """One run's facts + lazily derived views (rebuilt in memory, never persisted)."""

    def __init__(
        self,
        store: RunStore,
        meta: RunMetadata,
        results: list[CaseRunResult],
        report: dict[str, Any] | None,
        evals_root: Path,
    ) -> None:
        self.store = store
        self.meta = meta
        self.results = results
        self.report = report
        self.evals_root = evals_root
        self._comparison: RegressionComparison | None | object = _UNSET
        self._aggregate: RunAggregate | None = None
        self._analysis: AnalysisResult | None = None

    # -------------------------------------------------------------- aggregate

    @property
    def persisted(self) -> bool:
        """是否有已落盘的 report.json（UI 据此标注数字来源）。"""
        return self.report is not None

    def comparison(self) -> RegressionComparison | None:
        if self._comparison is _UNSET:
            self._comparison = self._load_comparison()
        return self._comparison  # type: ignore[return-value]

    def _load_comparison(self) -> RegressionComparison | None:
        persisted = (self.report or {}).get("regression")
        if persisted:
            try:
                return RegressionComparison.model_validate(persisted)
            except Exception:  # noqa: BLE001 — 老报告结构可能不完整，退回重算
                pass
        baseline_run_id = self.meta.baseline_run_id
        if not baseline_run_id:
            return None
        try:
            base_meta, base_results = self.store.load_run(baseline_run_id)
        except InvalidCallError:
            return None
        return compare_runs(base_meta, base_results, self.meta, self.results)

    def aggregate(self) -> RunAggregate:
        if self._aggregate is None:
            self._aggregate = build_aggregate(self.meta, self.results, self.comparison())
        return self._aggregate

    def gate(self) -> tuple[GateReport, str]:
        """返回 (report, source)；source ∈ {stored, replayed}。"""
        stored = (self.report or {}).get("gate")
        if stored:
            try:
                return GateReport.model_validate(stored), "stored"
            except Exception:  # noqa: BLE001
                pass
        path = self.store.report_path(self.meta.run_id, "gate.json")
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return GateReport.model_validate(payload), "stored"
            except Exception:  # noqa: BLE001
                pass
        rules = load_gate_rules(self.evals_root, "pr")
        return evaluate_gate(self.aggregate(), rules, self.comparison()), "replayed"

    def gate_exit_code(self, report: GateReport) -> int:
        return exit_code_for(report, self.aggregate())

    def analysis(self) -> AnalysisResult:
        """Failure 分类 + 聚类（规则引擎确定性输出，PRD §48）。"""
        if self._analysis is None:
            self._analysis = analyse_run(self.meta, self.results)
        return self._analysis

    def by_model(self) -> dict[str, int]:
        """PRD §78：按 Model 视角的 failure 计数（同一 run 只有一个 agent_model）。"""
        count = len(self.analysis().failures)
        if not count:
            return {}
        return {self.meta.agent_model or "unknown": count}

    def by_version(self) -> dict[str, int]:
        """PRD §78：按 Case version 视角的 failure 计数。"""
        counter: dict[str, int] = {}
        for failure in self.analysis().failures:
            key = f"v{failure.get('case_version', 1)}"
            counter[key] = counter.get(key, 0) + 1
        return counter

    def by_benchmark(self) -> dict[str, int]:
        failures = self.analysis().failures
        return {self.meta.benchmark_id: len(failures)} if failures else {}

    def artifacts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in ARTIFACT_NAMES:
            path = self.store.report_path(self.meta.run_id, name)
            if not path.is_file():
                continue
            out.append(
                {
                    "name": name,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "content_type": CONTENT_TYPES.get(name, "application/octet-stream"),
                    "url": f"/api/runs/{self.meta.run_id}/artifacts/{name}",
                }
            )
        return out

    def case_results(self) -> list[dict[str, Any]]:
        return [case.as_dict() for case in self.aggregate().cases]


_UNSET = object()


def load_view(store: RunStore, run_id: str, evals_root: Path) -> RunView:
    meta, results = store.load_run(run_id)
    report: dict[str, Any] | None = None
    path = store.report_path(run_id, "report.json")
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            report = payload if isinstance(payload, dict) else None
        except (json.JSONDecodeError, OSError):
            report = None
    return RunView(store, meta, results, report, evals_root)


def comparison_pairs(store: RunStore, limit: int = 200) -> list[dict[str, Any]]:
    """列出全部"有 baseline 引用的 run"（PRD §77 Regression UI 入口）。"""
    pairs: list[dict[str, Any]] = []
    for meta in store.list_runs():
        if not meta.baseline_run_id:
            continue
        pairs.append(
            {
                "run_id": meta.run_id,
                "benchmark_id": meta.benchmark_id,
                "dataset_version": meta.dataset_version,
                "baseline_run_id": meta.baseline_run_id,
                "baseline_mode": meta.baseline_mode,
                "started_at": meta.started_at.isoformat(),
                "verdict": None,
            }
        )
    pairs.sort(key=lambda p: str(p["started_at"]), reverse=True)
    return pairs[:limit]
