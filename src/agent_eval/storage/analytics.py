"""DuckDB 派生层（Spec §1.3 五层分层规则，PRD §79 V1 存储）。

原则：
  定义层 / 实验层 / 执行层 / 评测层 = 由 evals/ 与 .agent-eval/runs/ 投影而来的**物化视图**
  派生层（failures / clusters / baselines / reviews / gates）= 可由下三层随时重建

因此本模块不持有唯一事实：``rebuild()`` 永远是安全的（清空 + 重建）。

安全约定（硬约束）：本模块不接受任意 SQL 文本，也不把标识符或数据拼进查询。
每条语句都是**调用点上的字面量**，所有取值走参数绑定（``?``）。这既挡住注入，
也避免出现"命令行传入任意 SQL"这类越权读取路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from agent_eval.errors import InvalidCallError
from agent_eval.loading.loader import load_dataset
from agent_eval.models.results import CaseRunResult
from agent_eval.models.run import RunMetadata

TABLES = (
    "runs",
    "case_runs",
    "metric_results",
    "failures",
    "failure_clusters",
    "baselines",
    "human_reviews",
    "quality_gates",
    "experiments",
    "experiment_variants",
    "dataset_versions",
)


def _rows(cursor: Any) -> list[dict[str, Any]]:
    """Read a cursor into dicts. Takes no SQL, so no statement can be injected here."""
    columns = [d[0] for d in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


class Analytics:
    """DuckDB 投影层：派生层查询与 Historical Trend 的唯一入口。"""

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        """``read_only=True`` 供只读消费方（REST API）使用：不建库、不建表、不改文件。"""
        self.db_path = db_path
        self.read_only = read_only
        if read_only:
            self.conn = duckdb.connect(str(db_path), read_only=True)
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(db_path))
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Analytics:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ schema

    def _ensure_schema(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "run_id VARCHAR, benchmark_id VARCHAR, dataset_id VARCHAR, "
            "dataset_version VARCHAR, dataset_hash VARCHAR, profile VARCHAR, suite VARCHAR, "
            "status VARCHAR, verdict VARCHAR, baseline_mode VARCHAR, baseline_run_id VARCHAR, "
            "experiment_id VARCHAR, variant_id VARCHAR, agent_endpoint VARCHAR, "
            "agent_model VARCHAR, judge_model VARCHAR, git_commit VARCHAR, git_branch VARCHAR, "
            "started_at TIMESTAMP, finished_at TIMESTAMP, total_cases INTEGER, "
            "passed_cases INTEGER, failed_cases INTEGER, total_tokens BIGINT, total_cost DOUBLE)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS case_runs ("
            "case_run_id VARCHAR, run_id VARCHAR, case_id VARCHAR, case_version INTEGER, "
            "iteration INTEGER, status VARCHAR, failure_semantics VARCHAR, "
            "failure_category VARCHAR, latency_ms BIGINT, token_count BIGINT, cost DOUBLE, "
            "tool_calls INTEGER, trace_path VARCHAR)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS metric_results ("
            "metric_result_id VARCHAR, case_run_id VARCHAR, run_id VARCHAR, case_id VARCHAR, "
            "iteration INTEGER, metric VARCHAR, evaluator VARCHAR, score DOUBLE, "
            "threshold DOUBLE, verdict VARCHAR, blocking BOOLEAN, mount VARCHAR, turn INTEGER, "
            "reason VARCHAR)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS failures ("
            "failure_id VARCHAR, run_id VARCHAR, case_run_id VARCHAR, case_id VARCHAR, "
            "metric_result_id VARCHAR, category VARCHAR, reason VARCHAR, evidence VARCHAR)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS failure_clusters ("
            "cluster_id VARCHAR, run_id VARCHAR, label VARCHAR, category VARCHAR, size INTEGER, "
            "representative_case_id VARCHAR, common_tool_sequence VARCHAR, common_error VARCHAR)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS baselines ("
            "id VARCHAR, benchmark_id VARCHAR, dataset_version VARCHAR, mode VARCHAR, "
            "pinned_run_id VARCHAR, pinned_by VARCHAR, pinned_at TIMESTAMP, "
            "gate_evidence_run_id VARCHAR, note VARCHAR)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS human_reviews ("
            "id VARCHAR, run_id VARCHAR, case_id VARCHAR, reviewer VARCHAR, verdict VARCHAR, "
            "note VARCHAR, queue_reason VARCHAR, created_at TIMESTAMP)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS quality_gates ("
            "gate VARCHAR, run_id VARCHAR, verdict VARCHAR, baseline_mode VARCHAR, "
            "baseline_run_id VARCHAR, rules_json VARCHAR, created_at TIMESTAMP)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS experiments ("
            "experiment_id VARCHAR, name VARCHAR, benchmark_id VARCHAR, dataset_version VARCHAR, "
            "status VARCHAR, created_at TIMESTAMP, note VARCHAR)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS experiment_variants ("
            "variant_id VARCHAR, experiment_id VARCHAR, name VARCHAR, run_id VARCHAR, "
            "dimensions_json VARCHAR, status VARCHAR)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS dataset_versions ("
            "dataset_id VARCHAR, version VARCHAR, description VARCHAR, hash VARCHAR, "
            "cases INTEGER)"
        )

    # ------------------------------------------------------------------- counts

    def counts(self) -> dict[str, int]:
        return {
            "runs": int(self.conn.execute("SELECT count(*) FROM runs").fetchone()[0]),
            "case_runs": int(self.conn.execute("SELECT count(*) FROM case_runs").fetchone()[0]),
            "metric_results": int(
                self.conn.execute("SELECT count(*) FROM metric_results").fetchone()[0]
            ),
            "failures": int(self.conn.execute("SELECT count(*) FROM failures").fetchone()[0]),
            "failure_clusters": int(
                self.conn.execute("SELECT count(*) FROM failure_clusters").fetchone()[0]
            ),
            "baselines": int(self.conn.execute("SELECT count(*) FROM baselines").fetchone()[0]),
            "human_reviews": int(
                self.conn.execute("SELECT count(*) FROM human_reviews").fetchone()[0]
            ),
            "quality_gates": int(
                self.conn.execute("SELECT count(*) FROM quality_gates").fetchone()[0]
            ),
            "experiments": int(self.conn.execute("SELECT count(*) FROM experiments").fetchone()[0]),
            "experiment_variants": int(
                self.conn.execute("SELECT count(*) FROM experiment_variants").fetchone()[0]
            ),
            "dataset_versions": int(
                self.conn.execute("SELECT count(*) FROM dataset_versions").fetchone()[0]
            ),
        }

    def _clear(self) -> None:
        self.conn.execute("DELETE FROM runs")
        self.conn.execute("DELETE FROM case_runs")
        self.conn.execute("DELETE FROM metric_results")
        self.conn.execute("DELETE FROM failures")
        self.conn.execute("DELETE FROM failure_clusters")
        self.conn.execute("DELETE FROM baselines")
        self.conn.execute("DELETE FROM human_reviews")
        self.conn.execute("DELETE FROM quality_gates")
        self.conn.execute("DELETE FROM experiments")
        self.conn.execute("DELETE FROM experiment_variants")
        self.conn.execute("DELETE FROM dataset_versions")

    # ------------------------------------------------------------------ rebuild

    def rebuild(self, runs_root: Path, evals_root: Path | None = None) -> dict[str, int]:
        """从事实层重建全部表（Spec §1.3：派生层任何时候可重建）。"""
        self._clear()
        run_dirs = (
            sorted(p for p in runs_root.iterdir() if p.is_dir()) if runs_root.is_dir() else []
        )
        for run_dir in run_dirs:
            meta_path = run_dir / "run.json"
            if not meta_path.is_file():
                continue
            try:
                meta = RunMetadata.model_validate(_read_json(meta_path))
            except Exception:  # 撕裂写：跳过而不是让整次 rebuild 失败
                continue
            report = _read_json_optional(run_dir / "report.json")
            results: list[CaseRunResult] = []
            for path in sorted((run_dir / "case_runs").glob("*.json")):
                try:
                    results.append(CaseRunResult.model_validate(_read_json(path)))
                except Exception:
                    continue
            self._insert_run(meta, report, results)
        self._insert_state(runs_root.parent / "state")
        if evals_root is not None:
            self._insert_datasets(evals_root)
        return self.counts()

    def _insert_run(
        self, meta: RunMetadata, report: dict[str, Any] | None, results: list[CaseRunResult]
    ) -> None:
        verdict = (report or {}).get("verdict", "")
        passed = sum(1 for r in results if r.status.value == "PASS")
        failed = sum(1 for r in results if r.status.value == "FAIL")
        priced = [r for r in results if r.cost_known]
        self.conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                meta.run_id,
                meta.benchmark_id,
                meta.dataset_id,
                meta.dataset_version,
                meta.dataset_hash,
                meta.profile,
                meta.suite,
                meta.status.value,
                verdict,
                meta.baseline_mode,
                meta.baseline_run_id,
                meta.experiment_id,
                meta.variant_id,
                meta.agent_endpoint,
                meta.agent_model,
                meta.judge_model,
                meta.git_commit,
                meta.git_branch,
                meta.started_at,
                meta.finished_at,
                len({r.case_id for r in results}),
                passed,
                failed,
                sum(r.token_count for r in results),
                round(sum(r.cost for r in priced), 10) if priced else None,
            ],
        )
        for r in results:
            self.conn.execute(
                "INSERT INTO case_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    r.id,
                    meta.run_id,
                    r.case_id,
                    r.case_version,
                    r.iteration,
                    r.status.value,
                    r.failure_semantics.value if r.failure_semantics else None,
                    r.failure_category,
                    r.latency_ms,
                    r.token_count,
                    r.cost if r.cost_known else None,
                    len(r.tool_calls),
                    r.trace_path,
                ],
            )
            for m in r.all_metric_results:
                self.conn.execute(
                    "INSERT INTO metric_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        m.id,
                        r.id,
                        meta.run_id,
                        r.case_id,
                        r.iteration,
                        m.metric,
                        m.evaluator,
                        m.score,
                        m.threshold,
                        m.verdict,
                        m.blocking,
                        m.metadata.get("mount"),
                        m.metadata.get("turn"),
                        m.reason,
                    ],
                )
                if m.verdict in {"fail", "error"} and m.blocking:
                    self.conn.execute(
                        "INSERT INTO failures VALUES (?,?,?,?,?,?,?,?)",
                        [
                            f"fl-{m.id}",
                            meta.run_id,
                            r.id,
                            r.case_id,
                            m.id,
                            m.metadata.get("failure_category") or r.failure_category or m.metric,
                            m.reason or "",
                            json.dumps(m.metadata, ensure_ascii=False),
                        ],
                    )
        for case in (report or {}).get("cases", []):
            cluster = case.get("failure_cluster") or {}
            if not cluster:
                continue
            self.conn.execute(
                "INSERT INTO failure_clusters VALUES (?,?,?,?,?,?,?,?)",
                [
                    f"cl-{meta.run_id}-{cluster.get('cluster_id')}",
                    meta.run_id,
                    cluster.get("label"),
                    cluster.get("category"),
                    int(cluster.get("size", 0)),
                    cluster.get("representative_case_id") or case.get("case_id"),
                    cluster.get("common_tool_sequence"),
                    cluster.get("common_error"),
                ],
            )

    def _insert_state(self, state_root: Path) -> None:
        """作者态（baselines / human_reviews / experiments）投影。"""
        for record in _read_jsonl(state_root / "baselines.jsonl"):
            pinned = record.get("pinned_run_id")
            if not pinned:
                continue
            self.conn.execute(
                "INSERT INTO baselines VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    record.get("id"),
                    record.get("benchmark_id"),
                    record.get("dataset_version"),
                    record.get("mode"),
                    pinned,
                    record.get("pinned_by"),
                    record.get("pinned_at"),
                    record.get("gate_evidence_run_id"),
                    record.get("note", ""),
                ],
            )
        for record in _read_jsonl(state_root / "human_reviews.jsonl"):
            self.conn.execute(
                "INSERT INTO human_reviews VALUES (?,?,?,?,?,?,?,?)",
                [
                    record.get("id"),
                    record.get("run_id"),
                    record.get("case_id"),
                    record.get("reviewer"),
                    record.get("verdict"),
                    record.get("note", ""),
                    record.get("queue_reason"),
                    record.get("created_at"),
                ],
            )
        for experiment in _read_jsonl(state_root / "experiments.jsonl"):
            self.conn.execute(
                "INSERT INTO experiments VALUES (?,?,?,?,?,?,?)",
                [
                    experiment.get("id"),
                    experiment.get("name"),
                    experiment.get("benchmark_id"),
                    experiment.get("dataset_version"),
                    experiment.get("status"),
                    experiment.get("created_at"),
                    experiment.get("note", ""),
                ],
            )
            for variant in experiment.get("variants", []):
                self.conn.execute(
                    "INSERT INTO experiment_variants VALUES (?,?,?,?,?,?)",
                    [
                        variant.get("id"),
                        experiment.get("id"),
                        variant.get("name"),
                        variant.get("run_id"),
                        json.dumps(variant.get("dimensions", {}), ensure_ascii=False),
                        variant.get("status"),
                    ],
                )

    def _insert_datasets(self, evals_root: Path) -> None:
        """定义层投影：dataset_versions（Spec §1.3 定义层）。"""
        datasets_dir = evals_root / "datasets"
        if not datasets_dir.is_dir():
            return
        for dataset_dir in sorted(datasets_dir.iterdir()):
            if not (dataset_dir / "dataset.yaml").is_file():
                continue
            try:
                info, cases = load_dataset(evals_root, dataset_dir.name)
            except InvalidCallError:
                continue
            self.conn.execute(
                "INSERT INTO dataset_versions VALUES (?,?,?,?,?)",
                [info.id, info.version, info.description, info.hash, len(cases)],
            )

    # ------------------------------------------------------------------ writers

    def record_gate(self, report: Any) -> None:
        self.conn.execute(
            "INSERT INTO quality_gates VALUES (?,?,?,?,?,?,?)",
            [
                report.gate,
                report.run_id,
                report.verdict,
                report.baseline_mode,
                report.baseline_run_id,
                json.dumps([r.model_dump(mode="json") for r in report.rules], ensure_ascii=False),
                report.created_at,
            ],
        )

    def record_baseline(self, baseline: Any) -> None:
        mode = baseline.mode.value if hasattr(baseline.mode, "value") else baseline.mode
        self.conn.execute(
            "INSERT INTO baselines VALUES (?,?,?,?,?,?,?,?,?)",
            [
                baseline.id,
                baseline.benchmark_id,
                baseline.dataset_version,
                mode,
                baseline.pinned_run_id,
                baseline.pinned_by,
                baseline.pinned_at,
                baseline.gate_evidence_run_id,
                baseline.note,
            ],
        )

    def record_experiment(self, experiment: Any) -> None:
        self.conn.execute(
            "INSERT INTO experiments VALUES (?,?,?,?,?,?,?)",
            [
                experiment.id,
                experiment.name,
                experiment.benchmark_id,
                experiment.dataset_version,
                experiment.status,
                experiment.created_at,
                experiment.note,
            ],
        )
        for variant in experiment.variants:
            self.conn.execute(
                "INSERT INTO experiment_variants VALUES (?,?,?,?,?,?)",
                [
                    variant.id,
                    experiment.id,
                    variant.name,
                    variant.run_id,
                    json.dumps(variant.dimensions, ensure_ascii=False),
                    variant.status,
                ],
            )

    def record_review(self, review: Any) -> None:
        self.conn.execute(
            "INSERT INTO human_reviews VALUES (?,?,?,?,?,?,?,?)",
            [
                review.id,
                review.run_id,
                review.case_id,
                review.reviewer,
                review.verdict,
                review.note,
                review.queue_reason,
                review.created_at,
            ],
        )

    # ------------------------------------------------------------------ readers

    def list_runs(
        self,
        benchmark_id: str | None = None,
        dataset_version: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                "SELECT run_id, benchmark_id, dataset_id, dataset_version, profile, status, "
                "verdict, baseline_mode, baseline_run_id, experiment_id, variant_id, agent_model, "
                "git_commit, git_branch, started_at, finished_at, total_cases, passed_cases, "
                "failed_cases, total_tokens, total_cost FROM runs "
                "WHERE (? IS NULL OR benchmark_id = ?) AND (? IS NULL OR dataset_version = ?) "
                "ORDER BY started_at DESC LIMIT ?",
                [benchmark_id, benchmark_id, dataset_version, dataset_version, limit],
            )
        )

    def trend(
        self,
        benchmark_id: str | None = None,
        dataset_version: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """PRD §58 Historical Trend：按 commit/version/date/model 展示核心指标。"""
        return _rows(
            self.conn.execute(
                "SELECT run_id, benchmark_id, agent_model, git_commit, dataset_version, "
                "started_at, verdict, total_cases, passed_cases, failed_cases, "
                "CASE WHEN total_cases > 0 THEN CAST(passed_cases AS DOUBLE) / total_cases "
                "ELSE 0 END AS task_success, total_tokens, total_cost FROM runs "
                "WHERE (? IS NULL OR benchmark_id = ?) AND (? IS NULL OR dataset_version = ?) "
                "ORDER BY started_at ASC LIMIT ?",
                [benchmark_id, benchmark_id, dataset_version, dataset_version, limit],
            )
        )

    def metric_trend(self, metric: str, limit: int = 50) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                "SELECT r.run_id, r.benchmark_id, r.started_at, r.agent_model, "
                "avg(m.score) AS score, count(*) AS samples "
                "FROM metric_results m JOIN runs r ON r.run_id = m.run_id "
                "WHERE m.metric = ? AND m.score IS NOT NULL "
                "GROUP BY 1, 2, 3, 4 ORDER BY r.started_at ASC LIMIT ?",
                [metric, limit],
            )
        )

    def flaky_cases(self, limit: int = 100) -> list[dict[str, Any]]:
        """PRD §31/§32：同一 run 内同 case 的 PASS/FAIL 混合即候选 flaky。"""
        return _rows(
            self.conn.execute(
                "SELECT run_id, case_id, count(*) AS iterations, "
                "sum(CASE WHEN status = 'PASS' THEN 1 ELSE 0 END) AS passes, "
                "sum(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END) AS fails, "
                "sum(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS errors "
                "FROM case_runs GROUP BY run_id, case_id "
                "HAVING passes > 0 AND fails > 0 ORDER BY run_id DESC LIMIT ?",
                [limit],
            )
        )

    def run_detail(self, run_id: str) -> dict[str, Any]:
        runs = _rows(self.conn.execute("SELECT * FROM runs WHERE run_id = ?", [run_id]))
        if not runs:
            raise InvalidCallError(f"run not projected in analytics store: {run_id}")
        return {
            "run": runs[0],
            "case_runs": _rows(
                self.conn.execute(
                    "SELECT * FROM case_runs WHERE run_id = ? ORDER BY case_id, iteration",
                    [run_id],
                )
            ),
            "metric_results": _rows(
                self.conn.execute(
                    "SELECT * FROM metric_results WHERE run_id = ? "
                    "ORDER BY case_id, iteration, metric",
                    [run_id],
                )
            ),
        }

    def case_run_detail(self, run_id: str, case_id: str) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                "SELECT * FROM metric_results WHERE run_id = ? AND case_id = ? "
                "ORDER BY iteration, metric",
                [run_id, case_id],
            )
        )

    def gate_history(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                "SELECT gate, run_id, verdict, baseline_mode, baseline_run_id, created_at "
                "FROM quality_gates WHERE run_id = ? ORDER BY created_at",
                [run_id],
            )
        )

    def reviews(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                "SELECT id, run_id, case_id, reviewer, verdict, note, queue_reason, created_at "
                "FROM human_reviews WHERE (? IS NULL OR run_id = ?) ORDER BY created_at DESC",
                [run_id, run_id],
            )
        )


def open_readonly(db_path: Path) -> Analytics | None:
    """只读打开投影层；投影尚未构建（或文件被占用）时返回 None。

    只读消费方（``agent-eval serve``）不得因为一次查询就凭空建出一个空库：
    那会掩盖"派生层尚未 rebuild"这一事实，让 UI 显示成"平台里没有数据"。
    """
    if not db_path.is_file():
        return None
    try:
        return Analytics(db_path, read_only=True)
    except duckdb.Error:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_optional(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return _read_json(path)
    except json.JSONDecodeError:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
