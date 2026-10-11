"""P1-3 Trace Replay 测试：脱敏 → 重放 → 比较 → 落账。"""

from __future__ import annotations

import asyncio
import json
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
    async def test_replay_rejects_invalid_timeout(self, tmp_path: Path) -> None:
        """C03 回归：timeout_seconds 非法输入必须 400，绝不 500。"""
        async with self._client(tmp_path) as client:
            trace_id = await self._ingest(client)
            for bad in ("abc", -1, 0):
                response = await client.post(
                    f"/api/production/traces/{trace_id}/replay",
                    json={"agent_endpoint": "fake://", "timeout_seconds": bad},
                )
                assert response.status_code == 400, (bad, response.text)

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


class TestReplayJobApi:
    """§63：Replay 的 Job 路径（``/api/replay-runs``）——钉调度语义，不重复重放逻辑。

    重放本身（脱敏/比较/落账）由共享执行体覆盖（TestReplayApi 走同步端点那批）；
    这里钉的是 Job 化新引入的东西：202 受理、按 kind 隔离的列表与详情、失败翻译成
    failure_kind、以及**单一实现**（同步端点里不得再出现脱敏/重放编排）。
    """

    # Job 路径必须走注册表（没有 ad-hoc endpoint）：夹具自带一个 fake:// 的
    # local-fake profile——与 test_agent_profile_resolution 同一手法。
    @staticmethod
    def _client(tmp_path: Path) -> httpx.AsyncClient:
        import shutil

        evals_root = tmp_path / "evals"
        shutil.copytree(REPO / "evals", evals_root)
        (evals_root / "agents" / "local-fake.yaml").write_text(
            "id: local-fake\ndisplay_name: Local\nendpoint: fake://\nenabled: true\n",
            encoding="utf-8",
        )
        (evals_root / "agents" / "down.yaml").write_text(
            "id: down\ndisplay_name: Down\nendpoint: http://127.0.0.1:1\nenabled: true\n",
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
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    @staticmethod
    async def _wait_terminal(client: httpx.AsyncClient, job_id: str) -> dict:
        for _ in range(400):
            response = await client.get(f"/api/replay-runs/{job_id}")
            assert response.status_code == 200, response.text
            job = response.json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            await asyncio.sleep(0.05)
        raise AssertionError(f"job {job_id} did not settle")

    @pytest.mark.asyncio
    async def test_full_flow_records_and_serves_the_replay(self, tmp_path: Path) -> None:
        async with self._client(tmp_path) as client:
            trace_id = await TestReplayApi()._ingest(client)
            response = await client.post(
                "/api/replay-runs",
                json={
                    "agent_profile": "local-fake",
                    "trace_id": trace_id,
                    "policy": "production-qa",
                },
            )
            assert response.status_code == 202, response.text
            job = response.json()
            assert job["kind"] == "replay"
            assert job["status"] == "queued"
            # 重放不产生 Run：run_id 必须留空（用 run_id 表达会曲解语义）。
            assert job["run_id"] is None

            settled = await self._wait_terminal(client, job["job_id"])
            assert settled["status"] == "succeeded", settled
            assert settled["replay_id"].startswith("rp_")

            record = (
                await client.get(f"/api/production/replays/{settled['replay_id']}")
            ).json()
            assert record["status"] == "completed"
            assert record["trace_id"] == trace_id
            assert record["comparison"]["tool_sequence_original"] == ["order.search"]
            assert "evaluation_replay" in record["comparison"]

    @pytest.mark.asyncio
    async def test_lists_are_isolated_by_kind(self, tmp_path: Path) -> None:
        """评测列表里混进重放 Job 会让"这次评测跑了什么"变模糊（它没有 Run/gate）。"""
        async with self._client(tmp_path) as client:
            trace_id = await TestReplayApi()._ingest(client)
            created = await client.post(
                "/api/replay-runs",
                json={"agent_profile": "local-fake", "trace_id": trace_id},
            )
            assert created.status_code == 202
            job_id = created.json()["job_id"]
            await self._wait_terminal(client, job_id)

            replay_ids = {j["job_id"] for j in (await client.get("/api/replay-runs")).json()}
            assert job_id in replay_ids
            eval_ids = {j["job_id"] for j in (await client.get("/api/eval-runs")).json()}
            assert job_id not in eval_ids

    @pytest.mark.asyncio
    async def test_detail_rejects_a_job_of_another_kind(self, tmp_path: Path) -> None:
        """拿 eval Job 的 id 打 replay 详情会返回"没有 replay_id 的 succeeded Job"——
        读的人会以为重放成功了。kind 不符必须 404。"""
        async with self._client(tmp_path) as client:
            created = await client.post(
                "/api/eval-runs",
                json={
                    "agent_profile": "local-fake",
                    "benchmark": "database-core",
                    "tags": ["smoke"],
                },
            )
            assert created.status_code == 202, created.text
            eval_job_id = created.json()["job_id"]
            try:
                assert (
                    await client.get(f"/api/replay-runs/{eval_job_id}")
                ).status_code == 404
                assert (
                    await client.get(f"/api/replay-runs/{eval_job_id}/events")
                ).status_code == 404
            finally:
                await client.post(f"/api/eval-runs/{eval_job_id}/cancel")
            assert (await client.get("/api/replay-runs/job_nope")).status_code == 404

    @pytest.mark.asyncio
    async def test_failure_is_translated_not_silently_recorded(self, tmp_path: Path) -> None:
        """端点不可达 → Job FAILED + INFRA_FAILURE（不是"跑完了但记录里写着 error"）。"""
        async with self._client(tmp_path) as client:
            trace_id = await TestReplayApi()._ingest(client)
            created = await client.post(
                "/api/replay-runs",
                json={"agent_profile": "down", "trace_id": trace_id},
            )
            assert created.status_code == 202, created.text
            settled = await self._wait_terminal(client, created.json()["job_id"])
            assert settled["status"] == "failed", settled
            assert settled["failure_kind"] == "INFRA_FAILURE", settled
            assert settled["exit_code"] == 2, settled
            assert settled["replay_id"] is None
            # 失败也留了完整现场：ReplayStore 里那条记录必须是 error，不是半截成功。
            records = (await client.get("/api/production/replays")).json()
            assert any(r["status"] == "error" for r in records), records

    @pytest.mark.asyncio
    async def test_submit_time_validation(self, tmp_path: Path) -> None:
        """提交期就拒绝配置错（§13 纪律）：未知 policy、kind 不接受的字段、
        缺 trace_id、非法 trace_id 形状。"""
        async with self._client(tmp_path) as client:
            trace_id = await TestReplayApi()._ingest(client)
            base = {"agent_profile": "local-fake", "trace_id": trace_id}
            # 缺必填字段与 eval 侧缺 benchmark 同语义：pydantic 层 422。
            missing = await client.post("/api/replay-runs", json={"agent_profile": "local-fake"})
            assert missing.status_code == 422, missing.text
            for payload, why in (
                ({**base, "policy": "nope"}, "unknown policy"),
                ({**base, "trace_id": "../etc/passwd"}, "trace_id shape"),
                ({**base, "replay_timeout_seconds": 0}, "timeout must be positive"),
            ):
                response = await client.post("/api/replay-runs", json=payload)
                assert response.status_code == 400, (why, response.text)
            # 未知字段走 pydantic 校验层 → 422（不是语义 400）：重放请求里带
            # benchmark 属于客户端参数错配，必须显式拒绝而不是静默忽略。
            mixed = await client.post(
                "/api/replay-runs", json={**base, "benchmark": "database-core"}
            )
            assert mixed.status_code == 422, mixed.text

    @pytest.mark.asyncio
    async def test_unknown_trace_fails_the_job_as_invalid_request(self, tmp_path: Path) -> None:
        """trace 存在性只能执行期判定（提交期只知道形状）→ INVALID_REQUEST（exit 3），
        不是 INFRA_FAILURE——"请求里指了一个不存在的对象"与"环境坏了"必须分得开。"""
        async with self._client(tmp_path) as client:
            created = await client.post(
                "/api/replay-runs",
                json={"agent_profile": "local-fake", "trace_id": "prod_nonexistent"},
            )
            assert created.status_code == 202, created.text
            settled = await self._wait_terminal(client, created.json()["job_id"])
            assert settled["status"] == "failed", settled
            assert settled["failure_kind"] == "INVALID_REQUEST", settled
            assert settled["exit_code"] == 3, settled




    @staticmethod
    async def _ingest_slow(client: httpx.AsyncClient) -> str:
        """摄取一条输入带 [slow] 标记的 trace——fake adapter 会睡 3s 再答。

        给"取消一个**正在跑**的重放"提供确定性窗口（跟 database-core 的
        error.recovery.timeout 用例同一手法：prompt 里的标记触发脚本行为）。
        """
        events = [
            {
                "event_id": "e1",
                "trace_id": "t1",
                "type": "run.started",
                "data": {"input": "please [slow] down"},
            },
            {
                "event_id": "e2",
                "trace_id": "t1",
                "type": "run.finished",
                "data": {"output": "done", "status": "success"},
            },
        ]
        response = await client.post(
            "/api/production/traces",
            json={"source": "platform", "cases": [{"case_id": "conv-1", "events": events}]},
        )
        assert response.status_code == 201, response.text
        return response.json()["trace_id"]

    @pytest.mark.asyncio
    async def test_cancel_a_running_replay(self, tmp_path: Path) -> None:
        """§17 的承诺对 replay 同样成立：取消传到执行器，Job 以 cancelled 收场。

        附带钉两件取消路径的事实：不落 ReplayStore 记录（取消不是一次重放结果），
        以及列表端点的 trace_id 过滤参数（本用例顺带消费它）。
        """
        async with self._client(tmp_path) as client:
            trace_id = await self._ingest_slow(client)
            created = await client.post(
                "/api/replay-runs",
                json={
                    "agent_profile": "local-fake",
                    "trace_id": trace_id,
                    "replay_timeout_seconds": 30,
                },
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["job_id"]

            for _ in range(100):
                job = (await client.get(f"/api/replay-runs/{job_id}")).json()
                if job["status"] == "running":
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(f"job never started running: {job}")

            cancelled = await client.post(f"/api/replay-runs/{job_id}/cancel")
            assert cancelled.status_code == 202, cancelled.text

            settled = await self._wait_terminal(client, job_id)
            assert settled["status"] == "cancelled", settled
            assert settled["replay_id"] is None, settled
            records = (
                await client.get(f"/api/production/replays?trace_id={trace_id}")
            ).json()
            assert records == [], records

    @pytest.mark.asyncio
    async def test_sse_delivers_frames_for_replay_jobs(self, tmp_path: Path) -> None:
        """§18 帧契约对 replay 同样成立：快照帧 + job.activity（replay.*）+ 终态具名事件。

        只测"不存在的 id → 404"会让共享的 SSE 实现游离在重放链路之外——帧里
        有没有重放自己的进度事件，必须在真订阅里看一眼。
        """
        async with self._client(tmp_path) as client:
            trace_id = await self._ingest_slow(client)
            created = await client.post(
                "/api/replay-runs",
                json={
                    "agent_profile": "local-fake",
                    "trace_id": trace_id,
                    "replay_timeout_seconds": 30,
                },
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["job_id"]

            async with asyncio.timeout(60):
                async with client.stream(
                    "GET", f"/api/replay-runs/{job_id}/events"
                ) as response:
                    assert response.headers["content-type"].startswith("text/event-stream")
                    current_event = None
                    saw_activity = False
                    terminal = None
                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            current_event = line.removeprefix("event: ")
                        elif line.startswith("data: ") and current_event:
                            payload = json.loads(line.removeprefix("data: "))
                            assert payload["job_id"] == job_id
                            if current_event == "job.activity":
                                if '"replay.prepared"' in line:
                                    saw_activity = True
                            elif current_event in {
                                "job.completed",
                                "job.failed",
                                "job.cancelled",
                            }:
                                terminal = (current_event, payload)
                                break
            assert terminal is not None, "SSE 必须以具名终态事件收口"
            event, payload = terminal
            assert event == "job.completed"
            assert payload["status"] == "succeeded"
            assert payload["replay_id"], payload
            assert saw_activity, "replay.prepared 进度事件必须上流（job.activity）"

    @pytest.mark.asyncio
    async def test_notifications_feed_excludes_replay_jobs(self, tmp_path: Path) -> None:
        """§63 review：通知面是"评测结束"的投影——重放混进来只会得到 benchmark=null。

        NotificationItem.benchmark 在前端是 string；后端若发 null 就是类型谎言。
        """
        async with self._client(tmp_path) as client:
            trace_id = await TestReplayApi()._ingest(client)
            created = await client.post(
                "/api/replay-runs",
                json={"agent_profile": "local-fake", "trace_id": trace_id},
            )
            assert created.status_code == 202, created.text
            settled = await self._wait_terminal(client, created.json()["job_id"])
            assert settled["status"] == "succeeded", settled

            feed = (await client.get("/api/notifications")).json()
            assert created.json()["job_id"] not in {item["job_id"] for item in feed}

    @pytest.mark.asyncio
    async def test_idempotency_key_is_kind_scoped(self, tmp_path: Path) -> None:
        """§63 review：同 key 的异 kind 请求是配置错——显式 400，绝不拿 eval Job 冒充
        重放响应（读者会以为重放成功了）。同 key 同 kind 仍是 200 幂等重放（§36）。"""
        async with self._client(tmp_path) as client:
            trace_id = await TestReplayApi()._ingest(client)
            body = {"agent_profile": "local-fake", "trace_id": trace_id}
            first = await client.post(
                "/api/replay-runs", headers={"Idempotency-Key": "k-shared"}, json=body
            )
            assert first.status_code == 202, first.text
            duplicate = await client.post(
                "/api/replay-runs", headers={"Idempotency-Key": "k-shared"}, json=body
            )
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["job_id"] == first.json()["job_id"]

            cross = await client.post(
                "/api/eval-runs",
                headers={"Idempotency-Key": "k-shared"},
                json={
                    "agent_profile": "local-fake",
                    "benchmark": "database-core",
                    "tags": ["smoke"],
                },
            )
            assert cross.status_code == 400, cross.text
            assert "idempotency key" in cross.text

    def test_service_rejects_eval_side_fields_for_replay(self, tmp_path: Path) -> None:
        """API 的 extra="forbid" 只挡得住 HTTP；服务层直连要靠 _validate_replay 自己拦。

        review 发现旧守卫只拦了 benchmark/suite/profile 三个名字——repeat/no_judge
        这类字段仍会被静默收下并忽略，正是"声明了却不生效"的同族缺陷。
        """
        import shutil

        from agent_eval.execution.models import EvalRunRequest, JobKind
        from agent_eval.execution.service import InvalidSubmission

        evals_root = tmp_path / "evals"
        shutil.copytree(REPO / "evals", evals_root)
        (evals_root / "agents" / "local-fake.yaml").write_text(
            "id: local-fake\ndisplay_name: Local\nendpoint: fake://\nenabled: true\n",
            encoding="utf-8",
        )
        app = create_app(
            evals_root=evals_root,
            data_root=tmp_path / "data",
            fixtures_root=REPO / "fixtures",
        )
        service = app.state.eval_run_service
        with pytest.raises(InvalidSubmission) as excinfo:
            service.submit(
                EvalRunRequest(
                    agent_profile="local-fake",
                    kind=JobKind.REPLAY,
                    trace_id="prod_x",
                    repeat=3,
                    no_judge=True,
                )
            )
        message = str(excinfo.value)
        assert "repeat" in message and "no_judge" in message, message


def test_replay_logic_lives_only_in_the_shared_executor() -> None:
    """§63 单一实现的结构护栏：同步端点里不得再出现脱敏/重放编排。

    这条护栏防的是"修一处忘另一处"——两份实现漂移后，同一个重放在两个入口会给出
    不同结论，而报告上看不出谁对。可红：把 run_replay 的调用换回内联实现即失败。
    """
    src = (REPO / "src/agent_eval/api/routers/production.py").read_text(encoding="utf-8")
    assert "run_replay(" in src
    for forbidden in ("sanitize_text", "execute_replay(", "compare_with_original", "new_id("):
        assert forbidden not in src, f"重放编排必须只在 execution/replay_job.py：{forbidden}"
