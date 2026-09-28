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
from urllib.parse import quote

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

# case 级产物的 MIME：按扩展名判，判不出的按二进制（不假装是文本）。
CASE_CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".sql": "application/sql",
    ".log": "text/plain",
    ".html": "text/html",
    ".xml": "application/xml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".csv": "text/csv",
}

# case 产物按这个集合判"能否当文本预览"：不在集合里的一律走下载（可能是图片、
# sqlite 文件），硬按 utf-8 解会抛异常或替换成乱码，两种都算失真。
TEXTUAL_CASE_TYPES = frozenset(
    {
        "application/json",
        "application/x-ndjson",
        "text/plain",
        "text/markdown",
        "application/sql",
        "text/html",
        "application/xml",
        "text/yaml",
        "text/csv",
    }
)
MAX_PREVIEW_BYTES = 512 * 1024


def case_content_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return CASE_CONTENT_TYPES.get(suffix, "application/octet-stream")


def _case_artifact_url(
    run_id: str, case_id: str, iteration: int, name: str, *, raw: bool = False
) -> str:
    """case 产物端点 URL。name 按路径段转义：它可能含 ``/``（``files/src/app.py``）。

    只转义 ``/`` 之外的部分不行——``?`` / ``#`` / 空格都会截断 URL，所以逐段
    quote。这一步只是"URL 正确性"，不是安全边界：安全边界在索引查名（Spec §21.4）。

    原文端点用**平级前缀** ``artifact-raw/`` 而不是 ``.../raw`` 后缀：后缀会被
    name 吃掉歧义——agent 若在工作区根写出一个名为 ``raw`` 的文件，产物名就是
    ``files/raw``，其预览 URL ``/artifacts/files/raw`` 会被后缀路由解读成
    "``files`` 的原文"，于是"下载得到、预览 404"。前缀没有这个问题。
    """
    quoted = "/".join(quote(part, safe="") for part in name.split("/"))
    if raw:
        return (
            f"/api/runs/{run_id}/cases/{quote(case_id, safe='')}"
            f"/artifact-raw/{quoted}?iteration={iteration}"
        )
    return (
        f"/api/runs/{run_id}/cases/{quote(case_id, safe='')}"
        f"/artifacts/{quoted}?iteration={iteration}"
    )


def artifact_file_within(store: RunStore, run_id: str, relative: str) -> Path | None:
    """把索引里的 run 目录相对路径解析成真实文件，越界或不存在返回 None。

    先 ``resolve()`` 再判包含关系：顺序反了就没用了——symlink 只有在解析之后才
    看得出指向哪里。调用方传进来的相对路径来自**索引**（不是请求参数），但这里
    仍然重新判一次：索引本身来自 case_runs/*.json，那是磁盘上的数据，不是可信
    输入（Spec §21.3）。
    """
    run_dir = store.run_dir_for(run_id).resolve()
    try:
        candidate = (run_dir / relative).resolve()
    except OSError:
        return None
    if candidate != run_dir and run_dir not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


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

    # -------------------------------------------------- case-level artifacts

    def case_artifact_rows(self, case_id: str) -> list[dict[str, Any]]:
        """某 case 全部 iteration 的产物索引（PRD §90）。

        索引来源是 ``CaseRunResult.artifacts``：case_run_id 就是 ``CaseRunResult.id``，
        所以每条产物的归属是**推导出来的**，不需要第二份映射表——少一个漂移点。
        """
        rows: list[dict[str, Any]] = []
        for result in sorted(self.results, key=lambda r: r.iteration):
            if result.case_id != case_id:
                continue
            for record in result.artifacts:
                content_type = case_content_type(record.name)
                rows.append(
                    {
                        "case_run_id": result.id,
                        "case_id": result.case_id,
                        "iteration": result.iteration,
                        "name": record.name,
                        "kind": record.kind.value,
                        "path": record.path,
                        "bytes": record.bytes,
                        "collected_at": record.collected_at.isoformat(),
                        "truncated": record.truncated,
                        "note": record.note,
                        "content_type": content_type,
                        # 两个 URL 都由服务端拼好：让消费方（Web / 第三方）永远不需要
                        # 自己往 URL 上接字符串——`${url}/raw` 这类拼接会吃掉 query，
                        # 而这类 bug 只会在"第 2 个 iteration"上暴露。
                        "url": _case_artifact_url(
                            result.run_id, result.case_id, result.iteration, record.name
                        ),
                        "raw_url": _case_artifact_url(
                            result.run_id,
                            result.case_id,
                            result.iteration,
                            record.name,
                            raw=True,
                        ),
                    }
                )
        return rows

    def case_artifact_notes(self, case_id: str) -> list[str]:
        """采集期记账（去重保序）：有内容就说明这次少了某件产物。"""
        notes: list[str] = []
        for result in sorted(self.results, key=lambda r: r.iteration):
            if result.case_id != case_id:
                continue
            for note in result.artifact_notes:
                if note not in notes:
                    notes.append(note)
        return notes

    def find_case_artifact(
        self, case_id: str, iteration: int, name: str
    ) -> tuple[CaseRunResult, Any] | None:
        """按 (case, iteration, name) 在索引里查一条产物。

        查不到就返回 None → 端点回 404。**这一步就是路径穿越的闸门**：请求里带了
        ``../../etc/passwd`` 也只会查不到，因为没有任何一条索引的 name 长这样。
        """
        for result in self.results:
            if result.case_id != case_id or result.iteration != iteration:
                continue
            for record in result.artifacts:
                if record.name == name:
                    return result, record
        return None


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
