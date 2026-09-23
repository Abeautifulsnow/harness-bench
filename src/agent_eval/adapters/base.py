"""AgentAdapter 抽象（PRD §6.2）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from agent_eval.models.events import TraceEvent


class HealthStatus(BaseModel):
    ok: bool
    detail: str = ""


class SessionContext(BaseModel):
    """Eval 侧传给 Agent 的会话元数据（PRD §7.2）。"""

    eval_run_id: str
    case_id: str
    variant_id: str | None = None
    iteration: int = 1
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentSession(BaseModel):
    session_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    message: str
    stream: bool = True


class AgentFailureError(Exception):
    """Agent 通过事件流上报的错误（error 事件）→ AGENT_FAILURE（PRD §46）。"""


class AgentAdapter(ABC):
    @abstractmethod
    async def health_check(self) -> HealthStatus: ...

    @abstractmethod
    async def create_session(self, context: SessionContext) -> AgentSession: ...

    @abstractmethod
    def run(self, session: AgentSession, request: AgentRequest) -> AsyncIterator[TraceEvent]:
        """Stream agent events for one turn (1 turn = 1 run call, PRD §7.3)."""
        ...

    @abstractmethod
    async def cancel(self, session: AgentSession) -> None: ...
