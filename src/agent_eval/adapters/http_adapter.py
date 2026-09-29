"""HTTP + SSE AgentAdapter（PRD §6.1/§7：V1 接入协议）。

client 为惰性创建并复用（可注入测试用 MockTransport）；调 aclose() 释放。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import httpx

from agent_eval.adapters.base import (
    AgentAdapter,
    AgentRequest,
    AgentSession,
    HealthStatus,
    SessionContext,
)
from agent_eval.adapters.sse import parse_sse_stream
from agent_eval.errors import InfraError
from agent_eval.models.events import TraceEvent

DEFAULT_TIMEOUT = httpx.Timeout(30.0)


class HttpAgentAdapter(AgentAdapter):
    def __init__(
        self,
        endpoint: str,
        client: httpx.AsyncClient | None = None,
        health_timeout: float = 10.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._client = client  # injectable for tests (MockTransport / base_url required)
        self._owns = client is None
        self._health_timeout = health_timeout

    def _client_for(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._endpoint, timeout=DEFAULT_TIMEOUT)
            self._owns = True
        return self._client

    async def aclose(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> HealthStatus:
        try:
            resp = await self._client_for().get("/health", timeout=self._health_timeout)
            resp.raise_for_status()
            body = resp.json()
            return HealthStatus(
                ok=body.get("status") == "ok",
                detail=str(body),
                # A2/E2 + A4：观测面能力表（事件名 → bool）与实际生效模型在 health
                # 阶段整份上报；形状不对时按"未声明"处理（空表 = 按具备，不猜）。
                observation_surface=(
                    {str(k): bool(v) for k, v in body["observation_surface"].items()}
                    if isinstance(body.get("observation_surface"), dict)
                    else {}
                ),
                agent_model=(str(body["agent_model"]) if body.get("agent_model") else None),
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return HealthStatus(ok=False, detail=str(exc))

    async def create_session(self, context: SessionContext) -> AgentSession:
        # A1：SessionContext.extra 原样透传进 metadata（扩展键，E4 不透明）——
        # 协议面（PRD §7.2）允许 metadata 携带扩展键，workdir 语义由接入方定义。
        payload = {
            "metadata": {
                **context.extra,
                "eval_run_id": context.eval_run_id,
                "case_id": context.case_id,
                "variant_id": context.variant_id,
                "iteration": context.iteration,
            }
        }
        try:
            resp = await self._client_for().post("/api/agent/sessions", json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise InfraError(f"create_session failed on {self._endpoint}: {exc}") from exc
        session_id = body.get("session_id")
        if not session_id:
            raise InfraError("create_session response missing 'session_id'")
        # A1 修订四：workdir 可达性回执在**响应体**（AgentSession.metadata 只是
        # 请求回显，SUT 塞不进去）。缺失/非布尔 = 未回执（None），由 runner 按
        # "未知"记账——不默认视为可达。
        receipt = body.get("workdir_accessible")
        return AgentSession(
            session_id=str(session_id),
            metadata=payload["metadata"],
            workdir_accessible=receipt if isinstance(receipt, bool) else None,
        )

    def _stream_turn(self, session: AgentSession, request: AgentRequest) -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            client = self._client_for()
            async with client.stream(
                "POST",
                f"/api/agent/sessions/{session.session_id}/run",
                json=request.model_dump(),
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise InfraError(f"agent run HTTP {resp.status_code}: {body[:200]!r}")
                async for chunk in resp.aiter_bytes():
                    yield chunk

        return gen()

    def run(self, session: AgentSession, request: AgentRequest) -> AsyncIterator[TraceEvent]:
        return parse_sse_stream(self._stream_turn(session, request))

    async def cancel(self, session: AgentSession) -> None:
        with contextlib.suppress(httpx.HTTPError, OSError, asyncio.CancelledError):
            await self._client_for().post(
                f"/api/agent/sessions/{session.session_id}/cancel", timeout=10.0
            )  # best-effort teardown; the run outcome is already decided
