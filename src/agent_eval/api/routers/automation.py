"""Automation API（V2，docs §52：Scheduled Evaluation / Preset / Trigger）。

读端点照旧 GET；唯一的 POST 是触发器调用——它是给 CI / 外部系统的执行入口，
受执行 token 门（api/security.py）与触发器自身 token 的双重约束。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError

from agent_eval.api.security import enforce_exec_token
from agent_eval.execution.models import EvalRunJob, EvalRunRequest
from agent_eval.execution.presets import EvalPreset, PresetRequest, load_presets, resolve_request
from agent_eval.execution.scheduler import SchedulerService
from agent_eval.execution.service import DuplicateSubmission
from agent_eval.execution.triggers import TriggerView, load_triggers

router = APIRouter(tags=["automation"])


def _scheduler(request: Request) -> SchedulerService:
    return request.app.state.scheduler  # type: ignore[no-any-return]


@router.get(
    "/schedules",
    summary="定时评测调度列表（V2 §52：定义 + 调度台账视图）",
)
async def list_schedules(request: Request) -> list[dict]:
    return [view.model_dump(mode="json") for view in _scheduler(request).schedule_views()]


@router.get(
    "/eval-presets",
    response_model=list[EvalPreset],
    summary="评测预设/模板列表（V2 §52：Web 预填表单、Trigger 引用）",
)
async def list_eval_presets(request: Request) -> list[EvalPreset]:
    return load_presets(request.app.state.workspace.evals_root)


@router.get(
    "/triggers",
    response_model=list[TriggerView],
    summary="Webhook/CI 触发器列表（不含 token 值）",
)
async def list_triggers(request: Request) -> list[TriggerView]:
    out: list[TriggerView] = []
    for tdef in load_triggers(request.app.state.workspace.evals_root):
        out.append(
            TriggerView(
                id=tdef.id,
                display_name=tdef.display_name,
                enabled=tdef.enabled,
                description=tdef.description,
                preset=tdef.preset,
                token_ref=tdef.token_secret_ref,
                token_state=(
                    "none"
                    if tdef.token_secret_ref is None
                    else ("configured" if tdef.token_configured() else "missing")
                ),
            )
        )
    return out


@router.post(
    "/triggers/{trigger_id}/run",
    status_code=202,
    summary="调用触发器发起评测（CI/Webhook 入口；幂等重放 200）",
    dependencies=[Depends(enforce_exec_token)],
)
async def run_trigger(
    trigger_id: str,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EvalRunJob:
    workspace = request.app.state.workspace
    trigger = next((t for t in load_triggers(workspace.evals_root) if t.id == trigger_id), None)
    if trigger is None:
        raise HTTPException(status_code=404, detail=f"unknown trigger: {trigger_id}")
    if not trigger.enabled:
        raise HTTPException(status_code=409, detail=f"trigger '{trigger_id}' is disabled")

    token = (authorization or "").removeprefix("Bearer ").strip() or None
    if not trigger.verify_token(token):
        if trigger.token_configured():
            raise HTTPException(status_code=401, detail="trigger token required or invalid")
        # 声明了 token ref 但环境没配：这是配置问题（fail-closed 但要可解释）。
        raise HTTPException(
            status_code=400,
            detail=f"trigger token env '{trigger.token_secret_ref}' is not configured",
        )

    presets = {p.id: p for p in load_presets(workspace.evals_root)}
    base = presets[trigger.preset].request if trigger.preset else None
    if trigger.preset and base is None:
        raise HTTPException(status_code=400, detail=f"unknown preset: {trigger.preset}")
    try:
        payload = EvalRunRequest.model_validate(
            resolve_request(base or PresetRequest(), trigger.request)
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"trigger '{trigger_id}' resolves to an invalid request: {exc}",
        ) from exc

    try:
        job = request.app.state.eval_run_service.submit(
            payload,
            requested_by=f"trigger:{trigger.id}",
            idempotency_key=idempotency_key,
        )
    except DuplicateSubmission as dup:
        response.status_code = 200
        return dup.job
    response.status_code = 202
    return job
