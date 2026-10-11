"""Replay Run REST API（设计文档 §63：§53 Trace Replay 的执行面 Job 化）。

与 ``eval_runs`` 的关系：Job 账本、队列、取消、SSE 全部**共用**（§22/§25/§17/§18
按 job_id 寻址），本模块只提供 replay 侧的入口与自洽的读路径——前端不必知道
"重放 Job 的详情要去 eval-runs 拿"这种实现细节。

POST 在这里是新执行动词（§42：Execution mutation verbs = allowed）。它与
``/api/eval-runs`` 的区别只有一处：请求体的必填项是 trace_id 而不是 benchmark。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from agent_eval.api.routers.eval_runs import stream_job_events
from agent_eval.api.security import enforce_exec_token
from agent_eval.execution.models import (
    EvalRunJob,
    JobKind,
    ReplayRunRequest,
)
from agent_eval.execution.service import (
    DuplicateSubmission,
    EvalRunService,
    InvalidSubmission,
    JobAlreadyFinished,
)

router = APIRouter(tags=["replay-runs"])


def _service(request: Request) -> EvalRunService:
    return request.app.state.eval_run_service  # type: ignore[no-any-return]


def _replay_job(request: Request, job_id: str) -> EvalRunJob:
    """读路径守卫：只认 kind=replay 的 Job。

    不做这次判定的后果是真实的：拿一个 eval Job 的 id 打 replay 详情端点会返回
    一个"没有 replay_id、状态是 succeeded"的 Job，读的人会以为重放成功了。
    """

    job = _service(request).get(job_id)
    if job is None or job.kind is not JobKind.REPLAY:
        raise HTTPException(status_code=404, detail=f"unknown replay job: {job_id}")
    return job


@router.post(
    "/replay-runs",
    summary="创建 Trace Replay Job（§63：202 受理；幂等重放 200）",
    dependencies=[Depends(enforce_exec_token)],
)
async def create_replay_run(
    payload: ReplayRunRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    x_web_user: Annotated[str | None, Header()] = None,
) -> EvalRunJob:
    service = _service(request)
    try:
        job = service.submit(
            payload.to_request(),
            requested_by=(x_web_user or "local")[:64],
            idempotency_key=idempotency_key,
        )
    except DuplicateSubmission as dup:
        response.status_code = 200
        return dup.job
    except InvalidSubmission as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.status_code = 202
    return job


@router.get("/replay-runs", summary="Replay Job 列表（§63：新→旧，只含 kind=replay）")
async def list_replay_runs(request: Request) -> list[EvalRunJob]:
    return _service(request).list(kind=JobKind.REPLAY)


@router.get("/replay-runs/{job_id}", summary="Replay Job 详情（含 progress 与 replay_id）")
async def get_replay_run(job_id: str, request: Request) -> EvalRunJob:
    return _replay_job(request, job_id)


@router.post(
    "/replay-runs/{job_id}/cancel",
    status_code=202,
    summary="取消 Replay Job（§17：必须传到执行器）",
    dependencies=[Depends(enforce_exec_token)],
)
async def cancel_replay_run(job_id: str, request: Request) -> EvalRunJob:
    try:
        return _service(request).cancel(_replay_job(request, job_id).job_id)
    except JobAlreadyFinished as exc:
        raise HTTPException(status_code=409, detail=f"job already finished: {job_id}") from exc


@router.get("/replay-runs/{job_id}/events", summary="Replay Job 实时进度 SSE（§18）")
async def stream_replay_run_events(job_id: str, request: Request) -> StreamingResponse:
    _replay_job(request, job_id)  # 404 守卫（SSE 建立前就要拒掉不该看的 id）
    return await stream_job_events(job_id, request)
