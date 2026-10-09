"""P1-3 Trace Replay 测试：脱敏 → 重放 → 比较 → 落账。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent_eval.api.app import create_app
from agent_eval.errors import InfraError
from agent_eval.evaluators.online_eval import EvalPair
from agent_eval.runner.replay import compare_with_original, execute_replay
from agent_eval.storage.replay_store import ReplayStore
from agent_eval.trace.sanitize import sanitize_text

REPO = Path(__file__).resolve().parents[1]


class TestSanitize:
    def test_typed_placeholders_and_counts(self) -> None:
        text = "邮箱 user@a.com，手机 13812345678，key sk-abcdefghij1234567890"
        result, n = sanitize_text(text)
        assert "[EMAIL]" in result and "[PHONE]" in result and "[SECRET]" in result
        assert "user@a.com" not in result and "13812345678" not in result
        assert n == 3

    def test_plain_text_untouched(self) -> None:
        text = "帮我查一下昨天的订单为什么还没发货"
        assert sanitize_text(text) == (text, 0)

    def test_empty_string(self) -> None:
        assert sanitize_text("") == ("", 0)

    def test_long_hex_becomes_token(self) -> None:
        text = "session f" + "0" * 31 + " end"
        result, n = sanitize_text(text)
        assert "[TOKEN]" in result and n == 1


class TestReplayStore:
    def test_save_get_list_and_invalid_id(self, tmp_path: Path) -> None:
        store = ReplayStore(tmp_path)
        store.save({"replay_id": "rp_a", "trace_id": "t1", "status": "completed"})
        store.save({"replay_id": "rp_b", "trace_id": "t1", "status": "error"})
        assert store.get("rp_a")["replay_id"] == "rp_a"
        assert store.get("rp_none") is None
        assert [r["replay_id"] for r in store.list()] == ["rp_b", "rp_a"]
        from agent_eval.errors import InvalidCallError

        with pytest.raises(InvalidCallError):
            store.get("../escape")


class TestExecuteReplay:
    @pytest.mark.asyncio
    async def test_fake_endpoint_full_turn(self) -> None:
        events = await execute_replay(
            replay_id="rp_x", prompt="help me", endpoint="fake://", timeout_seconds=10
        )
        types = [e["type"] for e in events]
        assert "run.finished" in types
        assert all(e["case_id"] == "replay" for e in events)

    @pytest.mark.asyncio
    async def test_unsupported_endpoint_is_invalid_call(self) -> None:
        with pytest.raises(Exception) as exc_info:  # noqa: B017 - InvalidEndpointError
            await execute_replay(
                replay_id="rp_x", prompt="p", endpoint="ftp://x", timeout_seconds=5
            )
        assert "unsupported agent endpoint" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_unhealthy_endpoint_is_infra_error(self) -> None:
        from agent_eval.adapters.base import AgentAdapter, HealthStatus

        class DownAdapter(AgentAdapter):
            async def health_check(self):
                return HealthStatus(ok=False, detail="down")

            async def create_session(self, context):
                raise AssertionError("should not be reached")

            def run(self, session, request):
                raise AssertionError("should not be reached")

            async def cancel(self, session):
                pass

        import agent_eval.runner.replay as replay_mod

        original_open = replay_mod.open_adapter
        replay_mod.open_adapter = lambda endpoint, headers=None: DownAdapter()
        try:
            with pytest.raises(InfraError):
                await execute_replay(
                    replay_id="rp_x", prompt="p", endpoint="fake://", timeout_seconds=5
                )
        finally:
            replay_mod.open_adapter = original_open


class TestCompare:
    @pytest.mark.asyncio
    async def test_tools_and_output_comparison(self) -> None:
        original = EvalPair(
            case_id="c",
            input="q",
            actual_output="old answer",
            tools_called=["order.search"],
        )
        replay_events = [
            {"type": "tool.call", "data": {"name": "database_schema"}},
            {"type": "tool.call", "data": {"name": "order.search"}},
            {"type": "run.finished", "data": {"output": "new answer"}},
        ]
        comparison = await compare_with_original(
            original_pair=original, prompt="q", replay_events=replay_events
        )
        assert comparison["tool_sequence_original"] == ["order.search"]
        assert set(comparison["tool_sequence_replay"]) == {"database_schema", "order.search"}
        assert 0 < comparison["tool_sequence_jaccard"] < 1
        assert comparison["output_changed"] is True
        assert "evaluation_original" not in comparison  # 没给 policy 就不编分数

    @pytest.mark.asyncio
    async def test_policy_scores_both_sides(self) -> None:
        class StubAdapter:
            def version(self):
                return "stub"

            async def evaluate(self, metric_id, threshold, trace, model=None):
                return 0.9, "stub"

        from agent_eval.evaluators.online_eval import EvalPolicy, EvalPolicyMetric

        policy = EvalPolicy(
            id="qa", display_name="QA", metrics=[EvalPolicyMetric(id="m", threshold=0.5)]
        )
        original = EvalPair(case_id="c", input="q", actual_output="answer", tools_called=["t"])
        replay_events = [
            {"type": "tool.call", "data": {"name": "t"}},
            {"type": "run.finished", "data": {"output": "answer"}},
        ]
        comparison = await compare_with_original(
            original_pair=original,
            prompt="q",
            replay_events=replay_events,
            policy=policy,
            adapter=StubAdapter(),
        )
        assert comparison["evaluation_original"]["m"]["pass"] == 1
        assert comparison["evaluation_replay"]["m"]["pass"] == 1
        assert comparison["output_changed"] is False


class TestReplayApi:
    @staticmethod
    def _client(tmp_path: Path) -> httpx.AsyncClient:
        class StubAdapter:
            def version(self):
                return "stub"

            async def evaluate(self, metric_id, threshold, trace, model=None):
                return 0.9, "stub"

        app = create_app(
            evals_root=REPO / "evals",
            data_root=tmp_path / "data",
            fixtures_root=REPO / "fixtures",
            production_evaluator=StubAdapter(),
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def _ingest(self, client: httpx.AsyncClient) -> str:
        events = [
            {
                "event_id": "e1",
                "trace_id": "t1",
                "type": "run.started",
                "timestamp": "2026-10-09T10:00:00+08:00",
                "data": {"input": "contact user@example.com about the order"},
            },
            {
                "event_id": "e2",
                "trace_id": "t1",
                "type": "tool.call",
                "timestamp": "2026-10-09T10:00:01+08:00",
                "data": {"name": "order.search"},
            },
            {
                "event_id": "e3",
                "trace_id": "t1",
                "type": "run.finished",
                "timestamp": "2026-10-09T10:00:02+08:00",
                "data": {"output": "shipped", "status": "success"},
            },
        ]
        response = await client.post(
            "/api/production/traces",
            json={"source": "platform", "cases": [{"case_id": "conv-1", "events": events}]},
        )
        assert response.status_code == 201, response.text
        return response.json()["trace_id"]

    @pytest.mark.asyncio
    async def test_replay_full_flow(self, tmp_path: Path) -> None:
        from agent_eval.storage.production_store import ProductionStore

        async with self._client(tmp_path) as client:
            trace_id = await self._ingest(client)
            response = await client.post(
                f"/api/production/traces/{trace_id}/replay",
                json={"agent_endpoint": "fake://", "policy": "production-qa"},
            )
            assert response.status_code == 200, response.text
            record = response.json()
            assert record["status"] == "completed", record.get("error")
            assert record["sanitize"]["redactions"] == 1
            assert "[EMAIL]" in record["prompt"]
            assert record["source_input"].startswith("contact user@")
            assert record["comparison"]["tool_sequence_original"] == ["order.search"]
            assert "evaluation_replay" in record["comparison"]

            # 重放 trace 落了 ProductionStore（source=replay，origin 指回原 trace）
            store = ProductionStore(tmp_path / "data")
            replay_meta = store.get_meta(record["replay_trace_id"])
            assert replay_meta["source"] == "replay"
            assert replay_meta["origin"] == trace_id

            listed = (await client.get("/api/production/replays")).json()
            assert any(r["replay_id"] == record["replay_id"] for r in listed)
            detail = await client.get(f"/api/production/replays/{record['replay_id']}")
            assert detail.status_code == 200
            assert (await client.get("/api/production/replays/rp_none")).status_code == 404

    @pytest.mark.asyncio
    async def test_replay_error_paths(self, tmp_path: Path) -> None:
        async with self._client(tmp_path) as client:
            trace_id = await self._ingest(client)
            assert (
                await client.post("/api/production/traces/prod_none/replay", json={})
            ).status_code == 404
            assert (
                await client.post(f"/api/production/traces/{trace_id}/replay", json={})
            ).status_code == 400, "缺端点必须显式 400"
            assert (
                await client.post(
                    f"/api/production/traces/{trace_id}/replay",
                    json={"agent_endpoint": "fake://", "case_id": "nope"},
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/production/traces/{trace_id}/replay",
                    json={"agent_endpoint": "fake://", "agent_profile": "nope"},
                )
            ).status_code == 200, "显式 agent_endpoint 优先，profile 不再解析"
            assert (
                await client.post(
                    f"/api/production/traces/{trace_id}/replay",
                    json={"agent_endpoint": "fake://", "policy": "nope"},
                )
            ).status_code == 400

    @pytest.mark.asyncio
    async def test_agent_profile_resolution(self, tmp_path: Path) -> None:
        """agent_profile 走注册表：fake endpoint 的本地 profile 可直接解析。"""
        evals_root = tmp_path / "evals"
        import shutil

        shutil.copytree(REPO / "evals", evals_root)
        (evals_root / "agents" / "local-fake.yaml").write_text(
            "id: local-fake\ndisplay_name: Local\nendpoint: fake://\nenabled: true\n",
            encoding="utf-8",
        )

        class StubAdapter:
            def version(self):
                return "stub"

            async def evaluate(self, metric_id, threshold, trace, model=None):
                return 0.9, "stub"

        app = create_app(
            evals_root=evals_root,
            data_root=tmp_path / "data",
            fixtures_root=REPO / "fixtures",
            production_evaluator=StubAdapter(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            events = [
                {
                    "event_id": "e1",
                    "trace_id": "t1",
                    "type": "run.started",
                    "data": {"input": "q"},
                },
                {
                    "event_id": "e2",
                    "trace_id": "t1",
                    "type": "run.finished",
                    "data": {"output": "a"},
                },
            ]
            trace_id = (
                await client.post(
                    "/api/production/traces",
                    json={"source": "platform", "cases": [{"case_id": "c", "events": events}]},
                )
            ).json()["trace_id"]
            response = await client.post(
                f"/api/production/traces/{trace_id}/replay",
                json={"agent_profile": "local-fake"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["endpoint"] == "fake://"
