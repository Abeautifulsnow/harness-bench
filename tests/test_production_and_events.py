"""§18 细粒度事件 + §53 Production Trace Ingestion 测试。"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest

from agent_eval.api.app import create_app
from agent_eval.execution.events import JobEventHub
from agent_eval.storage.production_store import ProductionStore
from agent_eval.trace.otel import convert_otel

REPO = Path(__file__).resolve().parents[1]

LOCAL_AGENT = """\
id: local-fake
display_name: Local Fake Agent
endpoint: fake://
enabled: true
"""


@pytest.fixture()
def workspace(tmp_path: Path):
    evals_root = tmp_path / "evals"
    shutil.copytree(REPO / "evals", evals_root)
    shutil.copytree(REPO / "fixtures", tmp_path / "fixtures")
    (evals_root / "agents" / "local-fake.yaml").write_text(LOCAL_AGENT, encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()
    return evals_root, data_root


class TestJobEventHub:
    def test_publish_reaches_subscriber_across_threads(self) -> None:
        async def scenario() -> list[dict]:
            hub = JobEventHub()
            queue = hub.subscribe("job_x")
            received: list[dict] = []
            # 模拟执行线程：从另一个真实线程 publish
            import threading

            def publisher() -> None:
                for index in range(3):
                    hub.publish("job_x", {"type": "tool.call", "seq": index})

            thread = threading.Thread(target=publisher)
            thread.start()
            deadline = time.monotonic() + 5
            while len(received) < 3 and time.monotonic() < deadline:
                try:
                    received.append(await asyncio.wait_for(queue.get(), timeout=0.5))
                except TimeoutError:
                    continue
            thread.join()
            hub.unsubscribe("job_x", queue)
            return received

        received = asyncio.run(scenario())
        assert [event["seq"] for event in received] == [0, 1, 2]

    def test_unsubscribed_job_is_noop(self) -> None:
        hub = JobEventHub()
        hub.publish("job_none", {"type": "x"})  # 无订阅者：不抛


class TestFineGrainedEvents:
    """§18：Runner 生命周期事件经 service hub 到 API SSE。"""

    @staticmethod
    def _client(workspace) -> httpx.AsyncClient:
        evals_root, data_root = workspace
        app = create_app(
            evals_root=evals_root, data_root=data_root, fixtures_root=REPO / "fixtures"
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_sse_streams_case_and_turn_events(self, workspace) -> None:
        async with self._client(workspace) as client:
            body = {"agent_profile": "local-fake", "benchmark": "database-core", "tags": ["smoke"]}
            job = (await client.post("/api/eval-runs", json=body)).json()

            seen_types: set[str] = set()
            terminal_payload = None
            deadline = asyncio.get_event_loop().time() + 180
            async with client.stream("GET", f"/api/eval-runs/{job['job_id']}/events") as response:
                current_event = None
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        current_event = line.removeprefix("event: ").strip()
                    elif line.startswith("data: ") and current_event:
                        payload = json.loads(line.removeprefix("data: "))
                        if current_event == "job.activity":
                            seen_types.add(payload["type"])
                        elif current_event in {"job.completed", "job.failed", "job.cancelled"}:
                            terminal_payload = payload
                            break
                    if asyncio.get_event_loop().time() > deadline:
                        break
            assert terminal_payload is not None, "SSE 应以终态收口"
            # fake:// 的事件流至少要有 case 生命周期；有 turn 驱动就有 turn 事件
            assert "case.started" in seen_types, seen_types
            assert "case.completed" in seen_types, seen_types
            assert seen_types & {"turn.started", "turn.completed"}, seen_types

    @pytest.mark.asyncio
    async def test_service_hub_receives_run_created(self, workspace) -> None:
        async with self._client(workspace) as client:
            service = client._transport.app.state.eval_run_service  # type: ignore[attr-defined]
            body = {"agent_profile": "local-fake", "benchmark": "database-core", "tags": ["smoke"]}
            job = (await client.post("/api/eval-runs", json=body)).json()
            queue = service.hub.subscribe(job["job_id"])
            deadline = time.monotonic() + 60
            types: set[str] = set()
            while time.monotonic() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                types.add(event["type"])
                if event["type"] in {"job", "done"}:
                    break
                if "run.created" in types and len(types) > 1:
                    break
            assert "run.created" in types
            service.hub.unsubscribe(job["job_id"], queue)


class TestOtelConverter:
    def test_convert_minimal_otel(self) -> None:
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "chat-svc"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "abc123",
                                    "spanId": "span-1",
                                    "parentSpanId": "span-0",
                                    "name": "chat.completion",
                                    "startTimeUnixNano": "1717200000000000000",
                                    "endTimeUnixNano": "1717200000123456789",
                                    "attributes": [
                                        {
                                            "key": "gen_ai.request.model",
                                            "value": {"stringValue": "qwen3.5"},
                                        }
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        events = convert_otel(payload)
        assert len(events) == 1
        event = events[0]
        # 保真：span 名不映射进 §8 词汇表
        assert event["type"] == "chat.completion"
        assert event["trace_id"] == "abc123"
        assert event["parent_span_id"] == "span-0"
        assert event["data"]["duration_ms"] == pytest.approx(123.457)
        assert event["data"]["hints"]["gen_ai.request.model"] == "qwen3.5"

    def test_rejects_non_otel_and_bad_spans(self) -> None:
        from agent_eval.errors import InvalidCallError

        with pytest.raises(InvalidCallError):
            convert_otel({"foo": 1})
        with pytest.raises(InvalidCallError):
            convert_otel({"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "x"}]}]}]})


class TestProductionIngestion:
    """§53：摄取（token 门 / 双格式）+ 只读查看。"""

    @staticmethod
    def _client(workspace) -> httpx.AsyncClient:
        evals_root, data_root = workspace
        app = create_app(
            evals_root=evals_root, data_root=data_root, fixtures_root=REPO / "fixtures"
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_ingest_platform_events_and_view(self, workspace) -> None:
        events = [
            {
                "event_id": "e1",
                "trace_id": "t1",
                "type": "agent.started",
                "timestamp": "2026-10-03T10:00:00+08:00",
                "data": {},
            },
            {
                "event_id": "e2",
                "trace_id": "t1",
                "parent_span_id": "e1",
                "type": "tool.call",
                "timestamp": "2026-10-03T10:00:01+08:00",
                "data": {"name": "search"},
            },
        ]
        async with self._client(workspace) as client:
            response = await client.post(
                "/api/production/traces",
                json={"source": "platform", "cases": [{"case_id": "conv-1", "events": events}]},
            )
            assert response.status_code == 201, response.text
            trace_id = response.json()["trace_id"]

            rows = (await client.get("/api/production/traces")).json()
            assert rows[0]["trace_id"] == trace_id
            assert rows[0]["total_events"] == 2

            view = (await client.get(f"/api/production/traces/{trace_id}/trace")).json()
            assert view["span_count"] >= 1
            assert view["root"] is not None

    @pytest.mark.asyncio
    async def test_ingest_otel_payload(self, workspace) -> None:
        otel = {
            "traceId": "abc",
            "spanId": "s1",
            "name": "chat.completion",
            "startTimeUnixNano": "1717200000000000000",
            "endTimeUnixNano": "1717200000500000000",
        }
        async with self._client(workspace) as client:
            response = await client.post(
                "/api/production/traces",
                json={
                    "cases": [
                        {
                            "case_id": "otel-0",
                            **{"resourceSpans": [{"scopeSpans": [{"spans": [otel]}]}]},
                        },
                    ]
                },
            )
            assert response.status_code == 201, response.text
            trace_id = response.json()["trace_id"]
            view = (await client.get(f"/api/production/traces/{trace_id}/trace")).json()
            assert view["span_count"] >= 1

    @pytest.mark.asyncio
    async def test_ingest_rejects_invalid_events(self, workspace) -> None:
        async with self._client(workspace) as client:
            response = await client.post(
                "/api/production/traces",
                json={"cases": [{"case_id": "x", "events": [{"nope": True}]}]},
            )
            assert response.status_code == 400

            bad = await client.post("/api/production/traces", json={"cases": []})
            assert bad.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_trace_404(self, workspace) -> None:
        async with self._client(workspace) as client:
            assert (await client.get("/api/production/traces/prod_missing")).status_code == 404
            assert (
                await client.get("/api/production/traces/prod_missing/trace")
            ).status_code == 404


class TestProductionStoreUnit:
    def test_duplicate_trace_id_rejected(self, tmp_path: Path) -> None:
        store = ProductionStore(tmp_path)
        case = {"case_id": "c", "events": [{"event_id": "e", "trace_id": "t", "type": "x"}]}
        store.save(source="platform", cases=[case], trace_id="prod_dup")
        from agent_eval.errors import InvalidCallError

        with pytest.raises(InvalidCallError):
            store.save(source="platform", cases=[case], trace_id="prod_dup")

    def test_case_filter_preserves_order(self, tmp_path: Path) -> None:
        store = ProductionStore(tmp_path)
        store.save(
            source="platform",
            cases=[
                {"case_id": "a", "events": [{"event_id": "1", "trace_id": "t", "type": "x"}]},
                {"case_id": "b", "events": [{"event_id": "2", "trace_id": "t", "type": "x"}]},
            ],
        )
        assert [e["event_id"] for e in store.load_events("none", None)] == []
        events = store.load_events(store.list()[0]["trace_id"], "b")
        assert [e["case_id"] for e in events] == ["b"]


class TestTraceIdTraversal:
    """review #I01：trace_id 会拼进文件路径且 meta 原样透传——路径形态的 id
    必须按"不存在"处理（404），绝不落到 production/ 之外。"""

    @pytest.mark.asyncio
    async def test_traversal_ids_are_404_not_leaked(self, workspace) -> None:
        evals_root, data_root = workspace
        # 放一个受害者文件在 production/ 之外，穿越成功就会被读出来
        (data_root / "secret.json").write_text('{"hacked": true}', encoding="utf-8")
        app = create_app(
            evals_root=evals_root, data_root=data_root, fixtures_root=REPO / "fixtures"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for bad in ["..%2Fsecret", "../secret", "..", "a/b", "/etc/passwd"]:
                detail = await client.get(f"/api/production/traces/{bad}")
                assert detail.status_code == 404, (bad, detail.text)
                assert "hacked" not in detail.text
                view = await client.get(f"/api/production/traces/{bad}/trace")
                assert view.status_code == 404, bad

    def test_store_rejects_path_like_ids(self, tmp_path: Path) -> None:
        from agent_eval.errors import InvalidCallError

        store = ProductionStore(tmp_path)
        case = {"case_id": "c", "events": [{"event_id": "e", "trace_id": "t", "type": "x"}]}
        with pytest.raises(InvalidCallError):
            store.save(source="platform", cases=[case], trace_id="../escape")
        # store 层契约：非法 id 大声抛（HTTP 层转 404），不静默返回空
        with pytest.raises(InvalidCallError):
            store.get_meta("../escape")
        with pytest.raises(InvalidCallError):
            store.load_events("../escape")
