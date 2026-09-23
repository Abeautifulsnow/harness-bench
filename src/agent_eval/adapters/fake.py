"""进程内脚本化 Agent（测试与本地 demo 用，无需网络）。

行为与 dev/mock_server.py 保持同一套确定性语义（默认调用
database_schema + execute_sql，输出含 "QUERY COMPLETE"；消息内标记驱动变体：
[fail] / [forbidden] / [subagent] / "30 天"）。
测试可通过 rules= 注入自定义 ScriptTurn。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from agent_eval.adapters.base import (
    AgentAdapter,
    AgentRequest,
    AgentSession,
    HealthStatus,
    SessionContext,
)
from agent_eval.ids import new_id
from agent_eval.models.events import TraceEvent

BASE_TOOLS = ["database_schema", "execute_sql"]
BASE_OUTPUT = "QUERY COMPLETE: mock agent result"


@dataclass
class ScriptTurn:
    output: str = BASE_OUTPUT
    tools: list[str] = field(default_factory=lambda: list(BASE_TOOLS))
    tokens: int = 200
    fail: bool = False
    sleep_s: float = 0.0


def default_rules() -> dict[str, ScriptTurn]:
    return {
        "[fail]": ScriptTurn(fail=True),
        "[forbidden]": ScriptTurn(tools=[*BASE_TOOLS, "shell_exec"]),
        "[subagent]": ScriptTurn(tools=["web_search", *BASE_TOOLS]),
        "30 天": ScriptTurn(output=BASE_OUTPUT + "（已按最近 30 天过滤）"),
    }


@dataclass
class FakeAgentAdapter(AgentAdapter):
    default: ScriptTurn = field(default_factory=ScriptTurn)
    rules: dict[str, ScriptTurn] = field(default_factory=default_rules)
    # 逐次消费的脚本队列（测试 FLAKY / 错误序列）；空则回退 rules/default
    script_queue: list[ScriptTurn] = field(default_factory=list)
    # 供测试断言的观测记录
    created_sessions: list[SessionContext] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def _script_for(self, message: str) -> ScriptTurn:
        if self.script_queue:
            return self.script_queue.pop(0)
        for key, turn in self.rules.items():
            if key in message:
                return turn
        return self.default

    async def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="fake")

    async def create_session(self, context: SessionContext) -> AgentSession:
        self.created_sessions.append(context)
        return AgentSession(session_id=new_id("sess"), metadata={"fake": True})

    def run(self, session: AgentSession, request: AgentRequest) -> AsyncIterator[TraceEvent]:
        return self._run(session, request)

    async def _run(self, session: AgentSession, request: AgentRequest) -> AsyncIterator[TraceEvent]:
        script = self._script_for(request.message)
        trace_id = new_id("trace")
        if script.sleep_s:
            await asyncio.sleep(script.sleep_s)
        yield TraceEvent(
            event_id=new_id("evt"),
            trace_id=trace_id,
            parent_span_id=None,
            type="run.started",
            data={"message": request.message},
        )
        agent_span = new_id("span")
        yield TraceEvent(
            event_id=agent_span,
            trace_id=trace_id,
            parent_span_id=None,
            type="agent.started",
            data={},
        )
        for tool in script.tools:
            call_id = new_id("span")
            yield TraceEvent(
                event_id=call_id,
                trace_id=trace_id,
                parent_span_id=agent_span,
                type="tool.call",
                data={"name": tool, "arguments": {"query": request.message[:32]}},
            )
            yield TraceEvent(
                event_id=new_id("evt"),
                trace_id=trace_id,
                parent_span_id=call_id,
                type="tool.result",
                data={"status": "ok"},
            )
        llm_span = new_id("span")
        yield TraceEvent(
            event_id=llm_span,
            trace_id=trace_id,
            parent_span_id=agent_span,
            type="model.request",
            data={"model": "fake-model"},
        )
        yield TraceEvent(
            event_id=new_id("evt"),
            trace_id=trace_id,
            parent_span_id=llm_span,
            type="model.response",
            data={
                "text": script.output,
                "usage": {
                    "input_tokens": script.tokens // 2,
                    "output_tokens": script.tokens // 2,
                },
            },
        )
        if script.fail:
            yield TraceEvent(
                event_id=new_id("evt"),
                trace_id=trace_id,
                parent_span_id=agent_span,
                type="error",
                data={"message": "scripted agent failure"},
            )
        yield TraceEvent(
            event_id=new_id("evt"),
            trace_id=trace_id,
            parent_span_id=None,
            type="run.finished",
            data={"status": "error" if script.fail else "success", "output": script.output},
        )

    async def cancel(self, session: AgentSession) -> None:
        self.cancelled.append(session.session_id)
