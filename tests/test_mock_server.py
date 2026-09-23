"""mock server 集成测试：真实 TCP + SSE 全链路。"""

from __future__ import annotations

from agent_eval.adapters.base import AgentRequest, SessionContext
from agent_eval.adapters.http_adapter import HttpAgentAdapter
from agent_eval.dev.mock_server import serve


async def test_mock_server_end_to_end() -> None:
    server = serve("127.0.0.1", 0)  # 随机端口
    port = server.server_address[1]
    adapter = HttpAgentAdapter(f"http://127.0.0.1:{port}")
    try:
        assert (await adapter.health_check()).ok
        session = await adapter.create_session(
            SessionContext(eval_run_id="r", case_id="c", iteration=1)
        )
        events = [e async for e in adapter.run(session, AgentRequest(message="查询订单 30 天"))]
        types = [e.type for e in events]
        assert types[0] == "run.started"
        assert types[-1] == "run.finished"
        assert "tool.call" in types and "model.response" in types
        finished = events[-1]
        assert finished.data["status"] == "success"
        assert "30 天" in finished.data["output"]
    finally:
        server.shutdown()
