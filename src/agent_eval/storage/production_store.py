"""Production Trace 存储（§53 第一片：摄取与只读查看）。

目录约定：``<data_root>/production/<trace_id>/``
    meta.json    摄取事实（source/ingested_at/case 数/事件数/endpoint/model）
    events.jsonl 每行一条 TraceEvent 兼容 dict（append-only）

它是**事实源**（raw events 原样落盘）；派生视图（Span Tree、未来的 Online Eval
判定）随时可从它重建——与 runs/ 产物区"可重建派生层"同一哲学（Spec §1.3）。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from agent_eval.errors import InvalidCallError

# review #I01：trace_id 会拼进文件路径，且 meta 是**原样透传**的 dict——
# 不校验的话路径穿越等价于"任意 JSON 文件读取"。入口是唯一防线。
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _require_trace_id(trace_id: str) -> str:
    if not _TRACE_ID_RE.fullmatch(trace_id):
        raise InvalidCallError(f"invalid production trace id: {trace_id!r}")
    return trace_id


class ProductionStore:
    def __init__(self, data_root: Path) -> None:
        self.root = data_root / "production"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        *,
        source: str,
        cases: list[dict],
        endpoint: str | None = None,
        model: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        """cases: [{case_id, events: [TraceEvent 兼容 dict]}]。返回 trace_id。"""

        if not cases:
            raise InvalidCallError("production trace must contain at least one case")
        for case in cases:
            if not case.get("case_id") or not case.get("events"):
                raise InvalidCallError("each case needs non-empty case_id and events")

        trace_id = _require_trace_id(
            trace_id or f"prod_{int(time.time() * 1000):08x}{uuid.uuid4().hex[:8]}"
        )
        directory = self.root / trace_id
        if directory.exists():
            raise InvalidCallError(f"production trace already exists: {trace_id}")
        directory.mkdir(parents=True)

        meta = {
            "trace_id": trace_id,
            "source": source,
            "ingested_at": datetime.now().astimezone().isoformat(),
            # §61 Online Eval：case 级显式评测字段（input/expected_output/context）
            # 随 meta 落账——提取优先级最高的一级；缺省仍走事件提取。
            "cases": {
                str(case["case_id"]): {
                    "events": len(case["events"]),
                    "input": case.get("input"),
                    "expected_output": case.get("expected_output"),
                    "context": case.get("context"),
                }
                for case in cases
            },
            "total_events": sum(len(case["events"]) for case in cases),
            "endpoint": endpoint,
            "model": model,
        }
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with (directory / "events.jsonl").open("w", encoding="utf-8") as fh:
            for case in cases:
                for event in case["events"]:
                    fh.write(
                        json.dumps({**event, "case_id": str(case["case_id"])}, ensure_ascii=False)
                        + "\n"
                    )
        return trace_id

    def get_meta(self, trace_id: str) -> dict | None:
        _require_trace_id(trace_id)
        path = self.root / trace_id / "meta.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def load_events(self, trace_id: str, case_id: str | None = None) -> list[dict]:
        """读事件；case_id 给定时只返回该 case 的（保持落盘顺序）。"""

        _require_trace_id(trace_id)
        path = self.root / trace_id / "events.jsonl"
        if not path.is_file():
            return []
        events: list[dict] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if case_id is None or event.get("case_id") == case_id:
                    events.append(event)
        return events

    def list(self) -> list[dict]:
        out: list[dict] = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            meta = self.get_meta(directory.name)
            if meta is not None:
                out.append(meta)
        return out

    # ------------------------------------------------ Online Eval（§61）

    def save_evaluation(self, trace_id: str, result: dict) -> str:
        _require_trace_id(trace_id)
        directory = self.root / trace_id / "evaluations"
        directory.mkdir(parents=True, exist_ok=True)
        eval_id = str(result["evaluation_id"])
        path = directory / f"{eval_id}.json"
        tmp = path.with_suffix(".json.tmp")
        record = {
            **result,
            "trace_id": trace_id,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return eval_id

    def list_evaluations(self, trace_id: str) -> list[dict]:
        _require_trace_id(trace_id)
        directory = self.root / trace_id / "evaluations"
        if not directory.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(directory.glob("eval_*.json"), reverse=True):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out

    def get_evaluation(self, trace_id: str, evaluation_id: str) -> dict | None:
        _require_trace_id(trace_id)
        _require_trace_id(evaluation_id)
        path = self.root / trace_id / "evaluations" / f"{evaluation_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
