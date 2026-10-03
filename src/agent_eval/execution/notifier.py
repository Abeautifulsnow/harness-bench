"""Job 终态 webhook 通知（V2 Notification 的最小形态，docs §52）。

约定：**尽力而为 + 留痕**。通知失败绝不改写 Job 的终态，也绝不重试到阻塞
执行线程——每次尝试（成功或失败）追加进 ``<data_root>/state/notifications.jsonl``，
可事后核对。发送在独立守护线程里同步完成（V1 执行器是本地线程模型，
不值得为一次 HTTP POST 引入跨 loop 编排）。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import httpx

from agent_eval.execution.models import EvalRunJob

_TIMEOUT_SECONDS = 10.0


def job_payload(job: EvalRunJob) -> dict:
    """通知体：事实摘要，不含任何凭证与 case 级结果（那些走 Run Detail）。"""

    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "status": str(job.status),
        "gate_verdict": job.gate_verdict,
        "benchmark": job.request.benchmark,
        "requested_by": job.requested_by,
        "error": job.error,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def auth_headers(token_secret_ref: str | None) -> dict[str, str]:
    """通知凭证：声明了 secret_ref 时现场解析；env 缺失 = 不带头（接收方判定）。"""

    if not token_secret_ref:
        return {}
    token = os.environ.get(token_secret_ref)
    return {"Authorization": f"Bearer {token}"} if token else {}


def fire_and_forget(job: EvalRunJob, log_path: Path) -> None:
    """终态通知入口：不等结果；失败留痕于 jsonl，绝不影响 Job 终态。"""

    if not job.notify_webhook:
        return
    thread = threading.Thread(
        target=_deliver,
        args=(job.notify_webhook, job_payload(job), auth_headers(job.notify_token_ref), log_path),
        name=f"notify-{job.job_id}",
        daemon=True,
    )
    thread.start()


def _deliver(url: str, payload: dict, headers: dict[str, str], log_path: Path) -> None:
    record = {
        "url": url,
        "job_id": payload["job_id"],
        "attempted_at": datetime.now().astimezone().isoformat(),
    }
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"content-type": "application/json", **headers},
            timeout=_TIMEOUT_SECONDS,
        )
        record["outcome"] = "delivered" if response.is_success else f"HTTP {response.status_code}"
    except (httpx.HTTPError, OSError) as exc:
        record["outcome"] = f"error: {type(exc).__name__}: {exc}"
    _append_log(log_path, record)


def _append_log(log_path: Path, record: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # 通知日志写不进去不影响平台运行；此时本次尝试已尽力。
        pass
