"""Run 落盘（PRD §79 V1：JSONL + 本地目录；§109.2 增量写入）。

布局::

    <root>/<run_id>/run.json                        RunMetadata + 聚合
    <root>/<run_id>/case_runs/<case>.iter<N>.json   CaseRunResult（完成即写）
    <root>/<run_id>/traces/<case>.iter<N>.events.jsonl  Raw Trace（append-only）
    <root>/<run_id>/report.json / summary.md        终态产物
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.models.events import TraceEvent
from agent_eval.models.results import CaseRunResult
from agent_eval.models.run import RunMetadata, RunStatus


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_dir: Path | None = None

    # ---------- write side ----------

    def create_run(self, meta: RunMetadata) -> Path:
        self.run_dir = self.root / meta.run_id
        (self.run_dir / "case_runs").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "traces").mkdir(parents=True, exist_ok=True)
        self.save_meta(meta)
        return self.run_dir

    def save_meta(self, meta: RunMetadata) -> None:
        assert self.run_dir is not None
        _write_json(self.run_dir / "run.json", meta.model_dump(mode="json"))

    def save_case_run(self, result: CaseRunResult) -> None:
        assert self.run_dir is not None
        path = self.run_dir / "case_runs" / f"{_safe(result.case_id)}.iter{result.iteration}.json"
        _write_json(path, result.model_dump(mode="json"))

    def append_events(self, case_file_key: str, events: list[TraceEvent]) -> None:
        assert self.run_dir is not None
        path = self.run_dir / "traces" / f"{_safe(case_file_key)}.events.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")

    # ---------- read side ----------

    def load_run(self, run_id: str) -> tuple[RunMetadata, list[CaseRunResult]]:
        run_dir = self.root / run_id
        meta_path = run_dir / "run.json"
        if not meta_path.is_file():
            raise InvalidCallError(f"run not found: {run_id}")
        meta = RunMetadata.model_validate(_read_json(meta_path))
        results: list[CaseRunResult] = []
        case_dir = run_dir / "case_runs"
        if case_dir.is_dir():
            for path in sorted(case_dir.glob("*.json")):
                results.append(CaseRunResult.model_validate(_read_json(path)))
        return meta, results

    def run_dir_for(self, run_id: str) -> Path:
        return self.root / run_id

    def load_events(self, run_id: str, case_file_key: str) -> list[TraceEvent]:
        """Raw Trace 回放（PRD §52；Span Tree 可由事件流重建）。"""
        path = self.run_dir_for(run_id) / "traces" / f"{_safe(case_file_key)}.events.jsonl"
        if not path.is_file():
            return []
        events: list[TraceEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(TraceEvent.model_validate(json.loads(line)))
            except Exception:  # 撕裂写：跳过坏行，保留可解析部分
                continue
        return events

    def load_report(self, run_id: str) -> dict:
        path = self.run_dir_for(run_id) / "report.json"
        if not path.is_file():
            raise InvalidCallError(f"report not found for run: {run_id}")
        return _read_json(path)

    def report_path(self, run_id: str, name: str) -> Path:
        return self.run_dir_for(run_id) / name

    def list_runs(self) -> list[RunMetadata]:
        metas: list[RunMetadata] = []
        if not self.root.is_dir():
            return metas
        for run_dir in sorted(self.root.iterdir()):
            meta_path = run_dir / "run.json"
            if meta_path.is_file():
                try:
                    metas.append(RunMetadata.model_validate(_read_json(meta_path)))
                except Exception:  # torn write on a crashed run — skip, don't fail listing
                    continue
        return metas

    @staticmethod
    def set_status(meta: RunMetadata, status: RunStatus) -> RunMetadata:
        meta.status = status
        return meta


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
