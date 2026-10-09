"""Replay 记录存储（P1-3 Trace Replay）。

``<data_root>/replays/<replay_id>.json``（原子写）。replay 是派生事实（可从
原 trace + 定义重新执行），但执行结论（比较结果）不可重算出"当时那次"的
agent 行为，因此落账保留；与 runs/ 产物区同一哲学（Spec §1.3）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agent_eval.errors import InvalidCallError

# replay_id 会拼进文件路径——与 ProductionStore 同一道防线
_REPLAY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _require_replay_id(replay_id: str) -> str:
    if not _REPLAY_ID_RE.fullmatch(replay_id):
        raise InvalidCallError(f"invalid replay id: {replay_id!r}")
    return replay_id


class ReplayStore:
    def __init__(self, data_root: Path) -> None:
        self.root = data_root / "replays"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, record: dict) -> None:
        replay_id = _require_replay_id(str(record["replay_id"]))
        path = self.root / f"{replay_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, replay_id: str) -> dict | None:
        _require_replay_id(replay_id)
        path = self.root / f"{replay_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def list(self) -> list[dict]:
        out: list[dict] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        return out
