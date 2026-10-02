"""Agent Connection API（设计文档 §8/§9/§44 PR4）。

只读注册表视图 + Health Check。凭证永不出进程：视图里只有
secret_ref（env var 名）与 configured/missing 状态（§27）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from agent_eval.errors import InvalidCallError
from agent_eval.execution.connections import (
    AgentConnectionProfile,
    AgentConnectionView,
    AgentHealthReport,
    check_agent_health,
    load_agent_connections,
)

router = APIRouter(tags=["agent-connections"])


def _profiles(request: Request) -> dict[str, AgentConnectionProfile]:
    workspace = request.app.state.workspace
    return {p.id: p for p in load_agent_connections(workspace.evals_root)}


@router.get(
    "/agent-connections",
    response_model=list[AgentConnectionView],
    summary="已注册 Agent Connection 列表（§8：只给名字与状态，不给 Secret）",
)
async def list_agent_connections(request: Request) -> list[AgentConnectionView]:
    profiles = _profiles(request)
    return [AgentConnectionView.of(p) for p in profiles.values()]


@router.get(
    "/agent-connections/{profile_id}/health",
    response_model=AgentHealthReport,
    summary="Agent 健康探测（§9：/health + 观测面 + 实际模型）",
)
async def agent_connection_health(profile_id: str, request: Request) -> AgentHealthReport:
    profile = _profiles(request).get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown agent connection: {profile_id}")
    if not profile.enabled:
        raise HTTPException(status_code=409, detail=f"agent connection '{profile_id}' is disabled")
    try:
        return await check_agent_health(profile)
    except InvalidCallError as exc:
        # 凭证缺失（secret_ref 对应的 env var 不存在）——配置问题，不是 500。
        raise HTTPException(status_code=400, detail=str(exc.message)) from exc
