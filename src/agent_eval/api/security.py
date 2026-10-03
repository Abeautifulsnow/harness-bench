"""执行 token 门（V2 RBAC 的最小前置，docs §26"不要假设所有用户都是管理员"）。

可选硬门：设置环境变量 ``AGENT_EVAL_EXEC_TOKEN`` 后，全部执行动词
（发起/取消评测、触发器调用）要求 ``Authorization: Bearer <token>``；
未设置时保持 V1 的开放行为（本地工具）。恒定时间比较。
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

EXEC_TOKEN_ENV = "AGENT_EVAL_EXEC_TOKEN"


def exec_token_configured() -> bool:
    return bool(os.environ.get(EXEC_TOKEN_ENV))


def enforce_exec_token(request: Request) -> None:
    expected = os.environ.get(EXEC_TOKEN_ENV)
    if not expected:
        return
    supplied = request.headers.get("authorization", "")
    token = supplied.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="execution token required or invalid")
