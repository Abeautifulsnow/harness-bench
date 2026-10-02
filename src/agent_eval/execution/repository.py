"""Job Repository（设计文档 §22）：``<data_root>/jobs/`` 下的文件型账本。

V1 是 Local-first 单进程假设（§23）：文件即事实源，内存镜像只是加速读。
写路径沿用 storage 层的约定——tmp + replace 原子替换，crash 后不留半截 JSON。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_eval.execution.models import EvalRunJob


class JobRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, job: EvalRunJob) -> None:
        path = self.root / f"{job.job_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, job_id: str) -> EvalRunJob | None:
        # 与 list() 共用 _read：半截文件（进程在 replace 前被杀）按"读不到"
        # 处理，坏了哪条就少哪条，不让详情页 500。
        return _read(self.root / f"{job_id}.json")

    def list(self) -> list[EvalRunJob]:
        jobs = [job for path in self.root.glob("*.json") if (job := _read(path)) is not None]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def find_by_idempotency_key(self, key: str) -> EvalRunJob | None:
        for job in self.list():
            if job.idempotency_key == key:
                return job
        return None


def _read(path: Path) -> EvalRunJob | None:
    try:
        return EvalRunJob.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None
