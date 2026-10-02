"""Web Execution 控制面测试（docs/web-evaluation-control-plane-design.md §44/§48）。

测试策略与 test_api.py 同源：先有真实的定义树与 mock run，再让 API 说话。
Job 用 fake:// agent 全链路跑通（submit → running → succeeded → Run Detail 复用），
断言的都是端到端真值。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_eval.api.app import create_app
from agent_eval.execution.executor import LocalJobExecutor
from agent_eval.execution.models import EvalRunJob, EvalRunRequest, FailureKind, JobStatus
from agent_eval.execution.repository import JobRepository
from agent_eval.execution.service import EvalRunService

REPO = Path(__file__).resolve().parents[1]

LOCAL_AGENT = """\
id: local-fake
display_name: Local Fake Agent
endpoint: fake://
enabled: true
description: 测试用内置 mock
"""

BEARER_AGENT = """\
id: bearer-agent
display_name: Bearer Agent
endpoint: http://127.0.0.1:1
enabled: true
auth:
  type: bearer
  secret_ref: EXECUTION_TEST_AGENT_TOKEN
"""

DISABLED_AGENT = """\
id: disabled-agent
display_name: Disabled Agent
endpoint: fake://
enabled: false
"""


@pytest.fixture()
def workspace(tmp_path: Path):
    evals_root = tmp_path / "evals"
    shutil.copytree(REPO / "evals", evals_root)
    shutil.copytree(REPO / "fixtures", tmp_path / "fixtures")
    agents_dir = evals_root / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / "local-fake.yaml").write_text(LOCAL_AGENT, encoding="utf-8")
    (agents_dir / "bearer-agent.yaml").write_text(BEARER_AGENT, encoding="utf-8")
    (agents_dir / "disabled-agent.yaml").write_text(DISABLED_AGENT, encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()
    return evals_root, data_root


@pytest.fixture()
def client(workspace) -> TestClient:
    evals_root, data_root = workspace
    app = create_app(
        evals_root=evals_root,
        data_root=data_root,
        fixtures_root=Path(__file__).resolve().parents[1] / "fixtures",
        max_running_jobs=2,
    )
    return TestClient(app, raise_server_exceptions=False)


def _async_client(workspace, **kw) -> httpx.AsyncClient:
    evals_root, data_root = workspace
    app = create_app(
        evals_root=evals_root,
        data_root=data_root,
        fixtures_root=REPO / "fixtures",
        **kw,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _submit_body(**overrides) -> dict:
    payload = {
        "agent_profile": "local-fake",
        "benchmark": "database-core",
        "tags": ["smoke"],
    }
    payload.update(overrides)
    return payload


async def _wait_terminal(client: httpx.AsyncClient, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = (await client.get(f"/api/eval-runs/{job_id}")).json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        await asyncio.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s: {payload}")


class TestExecutionContract:
    def test_agent_connections_hide_secrets(self, client: TestClient) -> None:
        rows = client.get("/api/agent-connections").json()
        by_id = {row["id"]: row for row in rows}
        assert {"local-fake", "bearer-agent", "disabled-agent"} <= set(by_id)

        # §27：浏览器能看到 secret_ref（env var 名）与三态，看不到任何值。
        bearer = by_id["bearer-agent"]
        assert bearer["secret_ref"] == "EXECUTION_TEST_AGENT_TOKEN"
        assert bearer["secret_state"] == "missing"  # 测试环境没有这个 env
        # 出参字段集合钉死：多一个携带凭证值的字段都是契约破坏。
        assert set(bearer.keys()) == {
            "id",
            "display_name",
            "endpoint",
            "enabled",
            "description",
            "auth_type",
            "secret_ref",
            "secret_state",
        }
        assert "authorization" not in json.dumps(rows).lower()

        disabled = by_id["disabled-agent"]
        assert disabled["enabled"] is False

    def test_health_endpoint_reports_disabled_as_409(self, client: TestClient) -> None:
        response = client.get("/api/agent-connections/disabled-agent/health")
        assert response.status_code == 409


class TestSubmitValidation:
    """§48.1：invalid benchmark / invalid profile / disabled connection / 上限。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {"benchmark": "no-such-benchmark"},
            {"profile": "no-such-profile"},
            {"suite": ["no-such-suite"]},
            {"agent_profile": "no-such-connection"},
            {"agent_profile": "disabled-agent"},
            {"repeat": 0},
            {"repeat": 11},
            {"agent_concurrency": 0},
            {"agent_concurrency": 17},
        ],
    )
    async def test_invalid_submissions_are_400(self, workspace, payload: dict) -> None:
        async with _async_client(workspace) as client:
            response = await client.post("/api/eval-runs", json=_submit_body(**payload))
            assert response.status_code == 400, response.text
            # 400 的请求不能留下半截 Job。
            assert (await client.get("/api/eval-runs")).json() == []

    async def test_missing_secret_is_rejected_at_submit(self, workspace, monkeypatch) -> None:
        monkeypatch.delenv("EXECUTION_TEST_AGENT_TOKEN", raising=False)
        async with _async_client(workspace) as client:
            response = await client.post(
                "/api/eval-runs", json=_submit_body(agent_profile="bearer-agent")
            )
            assert response.status_code == 400
            # 应用的 HTTPException 处理器把 detail 包进 {"error": ...}。
            assert "EXECUTION_TEST_AGENT_TOKEN" in response.json()["error"]


class TestJobLifecycle:
    @pytest.mark.parametrize("field", ["benchmark", "profile"])
    @pytest.mark.parametrize("value", ["../evil", "/tmp/evil", "a/b", "..", "."])
    async def test_path_like_definition_names_are_rejected(
        self, workspace, field: str, value: str
    ) -> None:
        """review #I02：定义名会拼进文件路径，路径形态的名称必须在提交期 400。"""
        async with _async_client(workspace) as client:
            response = await client.post("/api/eval-runs", json=_submit_body(**{field: value}))
            assert response.status_code == 400, response.text
            assert (await client.get("/api/eval-runs")).json() == []

    async def test_terminal_state_cannot_be_revived(self, workspace) -> None:
        """review #I01：cancel() 的 get→_apply 两步之间 worker 可能已落终态；

        终态守卫必须丢弃迟到的 CANCELLING/progress，否则 Job 永久卡 cancelling。
        这里直接模拟竞态窗口的写入顺序。
        """
        evals_root, data_root = workspace
        app = create_app(
            evals_root=evals_root, data_root=data_root, fixtures_root=REPO / "fixtures"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            job = (await client.post("/api/eval-runs", json=_submit_body())).json()
            job = await _wait_terminal(client, job["job_id"])
            assert job["status"] == "succeeded"

            service: EvalRunService = app.state.eval_run_service
            service._apply(  # noqa: SLF001 - 测试的就是这条私有竞态路径
                job["job_id"], status=JobStatus.CANCELLING, cancel_requested=True
            )
            assert service.get(job["job_id"]).status == JobStatus.SUCCEEDED
            # 干净路径：对已终态 Job 的取消请求是 409，不是改写。
            cancel = await client.post(f"/api/eval-runs/{job['job_id']}/cancel")
            assert cancel.status_code == 409

    async def test_restart_reaps_orphaned_jobs(self, workspace) -> None:
        """review #S01：进程重启后 repo 里的非终态 Job 不再有人推进，必须诚实收口。"""
        evals_root, data_root = workspace
        repo = JobRepository(data_root / "jobs")
        repo.save(
            EvalRunJob(
                job_id="job_orphan",
                status=JobStatus.RUNNING,
                request=EvalRunRequest(agent_profile="local-fake", benchmark="database-core"),
            )
        )
        EvalRunService(evals_root=evals_root, data_root=data_root)
        reaped = repo.get("job_orphan")
        assert reaped.status == JobStatus.FAILED
        assert reaped.failure_kind == FailureKind.INTERNAL
        assert "restart" in (reaped.error or "")

    async def test_submit_run_complete(self, workspace) -> None:
        """§50 验收主链：202 → 后台独立运行 → succeeded → Run Detail 兼容。"""
        async with _async_client(workspace) as client:
            response = await client.post("/api/eval-runs", json=_submit_body())
            assert response.status_code == 202
            job = response.json()
            assert job["status"] in {"queued", "running"}
            assert job["run_id"] is None, "PREPARING 阶段不该有 run_id（§33）"

            job = await _wait_terminal(client, job["job_id"])
            assert job["status"] == "succeeded", job
            assert job["run_id"], "成功 Job 必须落 run_id（§50 结果段）"
            assert job["progress"]["completed_trials"] > 0
            assert job["progress"]["total_trials"] == job["progress"]["completed_trials"]

            # §48.3 / §50：Run Detail 与 CLI Run 完全兼容，Gate 读后端事实。
            run_id = job["run_id"]
            overview = (await client.get(f"/api/runs/{run_id}")).json()
            assert overview["verdict"] in {"pass", "fail", "undetermined"}
            gate = (await client.get(f"/api/gates/{run_id}")).json()
            assert gate["verdict"] == job["gate_verdict"]

    async def test_idempotent_resubmit_replays_same_job(self, workspace) -> None:
        async with _async_client(workspace) as client:
            headers = {"Idempotency-Key": "same-key-123"}
            first = await client.post("/api/eval-runs", json=_submit_body(), headers=headers)
            second = await client.post("/api/eval-runs", json=_submit_body(), headers=headers)
            assert first.status_code == 202
            assert second.status_code == 200
            assert first.json()["job_id"] == second.json()["job_id"]

            await _wait_terminal(client, first.json()["job_id"])

    async def test_cancel_running_job(self, workspace) -> None:
        async with _async_client(workspace) as client:
            response = await client.post("/api/eval-runs", json=_submit_body(repeat=5))
            job_id = response.json()["job_id"]

            # 等 Job 真正 RUNNING 再取消，命中"执行中取消"路径。
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = (await client.get(f"/api/eval-runs/{job_id}")).json()["status"]
                if status == "running":
                    break
                await asyncio.sleep(0.05)
            cancel = await client.post(f"/api/eval-runs/{job_id}/cancel")
            assert cancel.status_code == 202

            job = await _wait_terminal(client, job_id)
            # fake:// 跑得快，取消请求可能落在完成之后；两种结局都合法，
            # 但 cancelled 时必须带 cancel_requested 痕迹。
            if job["status"] == "cancelled":
                assert job["cancel_requested"] is True
                # §50：Runner 收到真实 cancellation——run.json 收口成 cancelled。
                if job["run_id"]:
                    run_status = (await client.get(f"/api/runs/{job['run_id']}/status")).json()
                    assert run_status["status"] == "cancelled"

    async def test_cancel_unknown_job_is_404(self, workspace) -> None:
        async with _async_client(workspace) as client:
            assert (await client.post("/api/eval-runs/job_missing/cancel")).status_code == 404
            assert (await client.get("/api/eval-runs/job_missing")).status_code == 404


class TestSseProgress:
    async def test_stream_reaches_terminal_event(self, workspace) -> None:
        """§18/§50：SSE 推 run-level progress，终态有具名事件收口。"""
        async with _async_client(workspace) as client:
            job = (await client.post("/api/eval-runs", json=_submit_body(repeat=3))).json()
            job_id = job["job_id"]

            async with asyncio.timeout(180):
                async with client.stream("GET", f"/api/eval-runs/{job_id}/events") as response:
                    assert response.headers["content-type"].startswith("text/event-stream")
                    current_event = None
                    terminal_payload = None
                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            current_event = line.removeprefix("event: ")
                        elif line.startswith("data: ") and current_event:
                            payload = json.loads(line.removeprefix("data: "))
                            assert payload["job_id"] == job_id
                            if current_event in {"job.completed", "job.failed", "job.cancelled"}:
                                terminal_payload = (current_event, payload)
                                break
            assert terminal_payload is not None
            event, payload = terminal_payload
            assert event == "job.completed"
            assert payload["status"] == "succeeded"
            assert payload["run_id"]


class TestLocalJobExecutor:
    """§24/§25 机制单测：全局并发=1 时排队、取消两条路径（与 Runner 解耦）。"""

    def test_queue_cancel_paths(self) -> None:
        events: list[tuple] = []
        executor = LocalJobExecutor(
            1,
            on_cancelled=lambda jid: events.append(("cancelled", jid)),
            on_error=lambda jid, exc: events.append(("error", jid, str(exc))),
        )

        async def sleeper() -> None:
            await asyncio.sleep(30)

        async def noop() -> None:
            pass

        try:
            executor.submit("a", sleeper)  # 占住唯一槽位
            executor.submit("b", noop)  # 压在信号量上（QUEUED）
            time.sleep(0.3)
            assert executor.is_in_flight("a")

            assert executor.cancel("b") is True
            assert executor.cancel("a") is True

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if {("cancelled", "a"), ("cancelled", "b")} <= set(events):
                    break
                time.sleep(0.05)
            assert {("cancelled", "a"), ("cancelled", "b")} <= set(events), events
            assert not any(e[0] == "error" for e in events)
        finally:
            executor.shutdown()
