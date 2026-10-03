"""站内通知（V2 Notification，docs §59）。

设计取舍：**不另建事件存储**。Job 账本（``<data_root>/jobs/*.json``）已经是
"哪些评测结束了"的唯一事实源——站内 feed 直接从终态 Job 投影，另存一份
notifications 事件表只会引入第二份事实与同步问题。要额外存的只有"已读"
标记（纯 UI 状态，不是事实）：``state/notification_state.json``。

已读集合有上限裁剪（保留最近 500 个已读 id）：防止长期运行下无限膨胀；
被裁掉的旧 id 重新显示为未读，可再标一次，无一致性风险。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_eval.execution.models import TERMINAL_STATUSES, EvalRunJob

_READ_MARK_CAP = 500


class NotificationStore:
    """已读标记：``<data_root>/state/notification_state.json``（原子写）。"""

    def __init__(self, data_root: Path) -> None:
        self.path = data_root / "state" / "notification_state.json"

    def read_job_ids(self) -> set[str]:
        if not self.path.is_file():
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(item) for item in raw.get("read_job_ids", [])}
        except (ValueError, OSError):
            return set()

    def mark_read(self, job_ids: list[str]) -> None:
        merged = sorted(self.read_job_ids() | set(job_ids))
        # 裁剪不追求精确 LRU，只要确定性：超限时丢最旧（id 序最小）的一批。
        if len(merged) > _READ_MARK_CAP:
            merged = merged[-_READ_MARK_CAP:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"read_job_ids": sorted(merged)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


def in_app_feed(
    jobs: list[EvalRunJob],
    store: NotificationStore,
    *,
    limit: int = 50,
) -> list[dict]:
    """终态 Job → 站内通知 feed（新→旧）。QUEUED/RUNNING/CANCELLING 不算通知。"""

    read_ids = store.read_job_ids()
    out: list[dict] = []
    for job in jobs:
        if job.status not in TERMINAL_STATUSES:
            continue
        out.append(
            {
                "job_id": job.job_id,
                "status": str(job.status),
                "run_id": job.run_id,
                "benchmark": job.request.benchmark,
                "gate_verdict": job.gate_verdict,
                "requested_by": job.requested_by,
                "error": job.error,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                "read": job.job_id in read_ids,
            }
        )
        if len(out) >= limit:
            break
    return out
