"""Agent 接入层（PRD §6）：adapter 抽象与实现注册。"""

from __future__ import annotations

from agent_eval.adapters.base import (
    AgentAdapter,
    AgentFailureError,
    AgentRequest,
    AgentSession,
    HealthStatus,
    SessionContext,
)
from agent_eval.adapters.fake import FakeAgentAdapter
from agent_eval.adapters.http_adapter import HttpAgentAdapter
from agent_eval.errors import InvalidCallError

__all__ = [
    "AgentAdapter",
    "AgentFailureError",
    "AgentRequest",
    "AgentSession",
    "FakeAgentAdapter",
    "HealthStatus",
    "HttpAgentAdapter",
    "SessionContext",
]


class InvalidEndpointError(InvalidCallError):
    """非法 agent endpoint：配置错误，属无效调用（Spec §6.1 exit 3，不归因于变更）。

    必须是 AgentEvalError 子类，否则会穿透 CLI 变成未捕获异常 → exit 1，
    让 CI 把配置错误误判为 PR 引入的回归。
    """


def open_adapter(endpoint: str, headers: dict[str, str] | None = None) -> AgentAdapter:
    """Resolve an adapter from an endpoint string.

    - ``fake://``            → in-process scripted adapter (tests / local dev)
    - ``http://...`` etc.    → HTTP + SSE adapter (PRD §6.1 V1 protocol)

    ``headers``：Agent Connection Profile 解析出的凭证头（Execution 文档 §27），
    仅对 HTTP 传输有意义；fake:// 没有传输层，静默忽略。
    """
    if endpoint == "fake://":
        return FakeAgentAdapter()
    if endpoint.startswith(("http://", "https://")):
        return HttpAgentAdapter(endpoint, headers=headers)
    raise InvalidEndpointError(
        f"unsupported agent endpoint '{endpoint}': expected 'fake://' or an http(s) URL"
    )
