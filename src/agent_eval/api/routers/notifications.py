"""站内通知 API（V2，docs §59）。

feed 是 Job 账本的投影（事实只有一份）；POST read 只改 UI 状态（已读标记），
不触碰 Definition 与评测事实——§42 边界的第三类合法 POST。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from agent_eval.execution.notifications import NotificationStore, in_app_feed

router = APIRouter(tags=["notifications"])


@router.get("/notifications", summary="站内通知 feed（终态 Job 投影，新→旧，含已读标记）")
async def list_notifications(request: Request) -> list[dict]:
    workspace = request.app.state.workspace
    jobs = request.app.state.eval_run_service.list()
    return in_app_feed(jobs, NotificationStore(workspace.data_root))


@router.post("/notifications/read", summary="标记通知已读（幂等；UI 状态，非评测事实）")
async def mark_notifications_read(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        # review #C01：非法 JSON 是 400，不能漏成 500。
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    job_ids = body.get("job_ids") if isinstance(body, dict) else None
    if not isinstance(job_ids, list) or not all(isinstance(item, str) for item in job_ids):
        raise HTTPException(status_code=400, detail='body must be {"job_ids": [string, ...]}')
    workspace = request.app.state.workspace
    NotificationStore(workspace.data_root).mark_read(job_ids)
    return {"marked_read": len(job_ids)}
