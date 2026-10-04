"""Execution REST API（设计文档 §16/§17/§18/§41/§42）。

新安全边界（§42）：``Definition mutation verbs = forbidden``，
``Execution mutation verbs = allowed``——本模块是整个 API 里唯一允许 POST 的地方，
且只允许"发起评测"与"取消评测"两个动作。Definition 数据仍然只能 GET。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from agent_eval.api.security import enforce_exec_token
from agent_eval.execution.models import TERMINAL_STATUSES, EvalRunJob, EvalRunRequest
from agent_eval.execution.service import (
    DuplicateSubmission,
    EvalRunService,
    InvalidSubmission,
    JobAlreadyFinished,
    UnknownJob,
)

router = APIRouter(tags=["eval-runs"])

# SSE 终态事件名（§18 的 job.completed / job.failed / job.cancelled）。
_TERMINAL_EVENTS = {
    "succeeded": "job.completed",
    "failed": "job.failed",
    "cancelled": "job.cancelled",
}
_SSE_POLL_SECONDS = 1.0


def _service(request: Request) -> EvalRunService:
    return request.app.state.eval_run_service  # type: ignore[no-any-return]


@router.post(
    "/eval-runs",
    summary="创建评测 Job（§16：202 受理；幂等重放 200）",
    dependencies=[Depends(enforce_exec_token)],
)
async def create_eval_run(
    payload: EvalRunRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    x_web_user: Annotated[str | None, Header()] = None,
) -> EvalRunJob:
    service = _service(request)
    try:
        job = service.submit(
            payload,
            # 审计字段（§37）限长：请求头不该原样灌进持久账本。
            requested_by=(x_web_user or "local")[:64],
            idempotency_key=idempotency_key,
        )
    except DuplicateSubmission as dup:
        # §36：同一 key 的重试返回既有 Job（200），绝不产生第二个相同评测。
        response.status_code = 200
        return dup.job
    except InvalidSubmission as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.status_code = 202
    return job


@router.get("/eval-runs", summary="评测 Job 列表（§38）")
async def list_eval_runs(request: Request) -> list[EvalRunJob]:
    return _service(request).list()


@router.get("/eval-runs/{job_id}", summary="评测 Job 详情（§16.2，含 progress）")
async def get_eval_run(job_id: str, request: Request) -> EvalRunJob:
    job = _service(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return job


@router.post(
    "/eval-runs/{job_id}/cancel",
    status_code=202,
    summary="取消评测（§17：必须传到 Runner）",
    dependencies=[Depends(enforce_exec_token)],
)
async def cancel_eval_run(job_id: str, request: Request) -> EvalRunJob:
    try:
        return _service(request).cancel(job_id)
    except UnknownJob as exc:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}") from exc
    except JobAlreadyFinished as exc:
        raise HTTPException(status_code=409, detail=f"job already finished: {job_id}") from exc


@router.get("/eval-runs/{job_id}/events", summary="Job 实时进度 SSE（§18：run-level）")
async def stream_eval_run_events(job_id: str, request: Request) -> StreamingResponse:
    service = _service(request)
    if service.get(job_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")

    async def events() -> Any:
        # §18：两条流合一——run-level 快照（轮询，事实源）+ case/turn/tool 级
        # 细粒度事件（hub 订阅，best-effort）。事件名 = 事件 type 本身
        # （case.started / tool.call / ...），Web 端按名字分发。
        queue = service.hub.subscribe(job_id)
        last_snapshot: str | None = None
        try:
            while True:
                job = service.get(job_id)
                if job is None:
                    break
                snapshot = job.model_dump_json()
                if snapshot != last_snapshot:
                    event = _TERMINAL_EVENTS.get(str(job.status), "job.updated")
                    yield f"event: {event}\ndata: {snapshot}\n\n"
                    last_snapshot = snapshot
                if job.status in TERMINAL_STATUSES:
                    break
                try:
                    fine_event = await asyncio.wait_for(queue.get(), timeout=_SSE_POLL_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    continue
                # EventSource 无法通配监听：细粒度事件统一挂 job.activity 名字，
                # 具体 type（case.started / tool.call / ...）在 data 里。
                yield "event: job.activity\n" + (
                    f"data: {json.dumps(fine_event, ensure_ascii=False, default=str)}\n\n"
                )
        finally:
            service.hub.unsubscribe(job_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
