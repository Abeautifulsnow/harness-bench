"""共享依赖与错误映射。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from agent_eval.api.workspace import Workspace
from agent_eval.errors import AgentEvalError, InvalidCallError


def get_workspace(request: Request) -> Workspace:
    return request.app.state.workspace


WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]


def not_found(exc: AgentEvalError) -> HTTPException:
    """Spec §6.1 exit 3 = 无效调用：REST 侧对应 404（对象不存在）/ 400（参数非法）。"""
    status = 404 if isinstance(exc, InvalidCallError) else 400
    return HTTPException(status_code=status, detail=exc.message)


def missing_projection_hint(path: str) -> str:
    return (
        f"派生层尚未构建（{path} 不存在）。REST API 只读，不会隐式建库："
        "请先执行 `agent-eval storage rebuild`。"
    )
