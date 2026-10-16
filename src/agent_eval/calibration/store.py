"""校准台账：``<state_root>/calibrations/<cal_id>.json``（Spec §26.1）。

一 set 一文件：items、标注、快照历史都是同一个集合的事实，分文件只会造出
"set 与 labels 漂移"的第二事实源。写入 tmp + replace 原子替换（与 RunStore 同纪律）。
读取侧对 cal_id 做白名单校验——它最终拼进文件路径，是输入不是可信数据。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.models.calibration import CalibrationSet

_CAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe(cal_id: str) -> str:
    if not _CAL_ID_PATTERN.fullmatch(cal_id):
        raise InvalidCallError(f"invalid calibration id '{cal_id}'")
    return cal_id


class CalibrationStore:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root / "calibrations"

    def save_set(self, cal_set: CalibrationSet) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{_safe(cal_set.cal_id)}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(cal_set.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def load_set(self, cal_id: str) -> CalibrationSet:
        path = self.root / f"{_safe(cal_id)}.json"
        if not path.is_file():
            raise InvalidCallError(f"calibration set '{cal_id}' 不存在")
        return CalibrationSet.model_validate_json(path.read_text(encoding="utf-8"))

    def list_sets(self) -> list[CalibrationSet]:
        if not self.root.is_dir():
            return []
        out: list[CalibrationSet] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                out.append(CalibrationSet.model_validate_json(path.read_text(encoding="utf-8")))
            except (ValueError, InvalidCallError):
                continue
        # cal_id 是随机 uuid，文件名排序无语义；按创建时间新在前
        return sorted(out, key=lambda cal_set: cal_set.created_at, reverse=True)
