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
from agent_eval.execution.service import EvalRunService, InvalidSubmission

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

    @pytest.mark.parametrize(
        "payload",
        [
            {"baseline_policy": "yolo"},
            {"baseline_policy": "explicit", "baseline_run": None},
            {"baseline_policy": "explicit"},
        ],
    )
    async def test_invalid_baseline_submission_is_400(self, workspace, payload: dict) -> None:
        """§31：baseline 策略白名单；explicit 必须指名 baseline run。"""
        async with _async_client(workspace) as client:
            response = await client.post("/api/eval-runs", json=_submit_body(**payload))
            assert response.status_code == 400, response.text

    async def test_valid_baseline_policy_passes_validation(self, workspace) -> None:
        """NO_BASELINE / main-latest 合法（提交成功即可，不要求跑完）。"""
        async with _async_client(workspace) as client:
            response = await client.post(
                "/api/eval-runs",
                json=_submit_body(baseline_policy="NO_BASELINE"),
            )
            assert response.status_code == 202


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

    def test_ordered_in_flight_preserves_submission_order(self) -> None:
        """§25 排队位次的依据：ordered_in_flight 必须按提交序返回。"""
        executor = LocalJobExecutor(2)

        async def sleeper() -> None:
            await asyncio.sleep(30)

        try:
            for job_id in ("first", "second", "third"):
                executor.submit(job_id, sleeper)
            time.sleep(0.3)
            assert executor.ordered_in_flight() == ["first", "second", "third"]
            assert executor.in_flight_ids() == {"first", "second", "third"}
        finally:
            executor.shutdown()


class TestQueuePosition:
    """§25：QUEUED Job 的排队位次是读时事实——get() 动态补，永不落盘。"""

    def _service_with_stub_executor(self, workspace, in_flight: list[str]):
        evals_root, data_root = workspace
        service = EvalRunService(
            evals_root=evals_root,
            data_root=data_root,
            fixtures_root=REPO / "fixtures",
            max_running_jobs=1,
        )

        class StubExecutor:
            """§23 JobExecutor 协议的最小替身：只提供位次计算需要的视图。"""

            def __init__(self, order: list[str]) -> None:
                self.order = order

            def ordered_in_flight(self) -> list[str]:
                return self.order

            def submit(self, job_id, body) -> None: ...

            def cancel(self, job_id: str) -> bool:
                return False

            def is_in_flight(self, job_id: str) -> bool:
                return job_id in self.order

            def in_flight_ids(self) -> set[str]:
                return set(self.order)

            def shutdown(self) -> None: ...

        service.executor = StubExecutor(in_flight)  # type: ignore[assignment]
        return service

    def test_queued_position_counts_running_and_ahead(self, workspace) -> None:
        from agent_eval.execution.models import EvalRunJob, JobStatus

        service = self._service_with_stub_executor(workspace, ["running-job", "queued-job"])
        running = EvalRunJob(
            job_id="running-job",
            status=JobStatus.RUNNING,
            request=EvalRunRequest(agent_profile="local-fake", benchmark="database-core"),
        )
        queued = EvalRunJob(
            job_id="queued-job",
            status=JobStatus.QUEUED,
            request=EvalRunRequest(agent_profile="local-fake", benchmark="database-core"),
        )
        service._jobs.update({"running-job": running, "queued-job": queued})  # noqa: SLF001

        assert service.get("queued-job").queue_position == 1  # 1 个在跑，它是下一个
        # 非 QUEUED 状态不补位次
        assert service.get("running-job").queue_position is None

    def test_queue_position_never_persisted(self, workspace) -> None:
        from agent_eval.execution.models import EvalRunJob, JobStatus

        evals_root, data_root = workspace
        repo = JobRepository(data_root / "jobs")
        job = EvalRunJob(
            job_id="job_q",
            status=JobStatus.QUEUED,
            request=EvalRunRequest(agent_profile="local-fake", benchmark="database-core"),
            queue_position=3,
        )
        repo.save(job)
        stored = repo.get("job_q")
        assert stored.queue_position is None, "位次是读时事实，落盘只会留过期快照"


class TestReapAndCaps:
    """review #I02/#I03：收割开关与上限解禁的语义锁定。"""

    def test_reap_orphans_false_leaves_jobs_untouched(self, workspace) -> None:
        evals_root, data_root = workspace
        repo = JobRepository(data_root / "jobs")
        repo.save(
            EvalRunJob(
                job_id="job_live",
                status=JobStatus.RUNNING,
                request=EvalRunRequest(agent_profile="local-fake", benchmark="database-core"),
            )
        )
        # CLI 场景：新建 service（reap 关）不得误标共享账本里活着的 Job。
        EvalRunService(evals_root=evals_root, data_root=data_root, reap_orphans=False)
        assert repo.get("job_live").status == JobStatus.RUNNING
        # 对照组：默认 reap 开（serve 装配语义）→ 收口。
        EvalRunService(evals_root=evals_root, data_root=data_root)
        assert repo.get("job_live").status == JobStatus.FAILED

    def test_caps_none_lifts_web_limits(self, workspace) -> None:
        evals_root, data_root = workspace
        service = EvalRunService(
            evals_root=evals_root,
            data_root=data_root,
            repeat_cap=None,
            agent_concurrency_cap=None,
        )
        request = EvalRunRequest(
            agent_profile="local-fake", benchmark="database-core", repeat=999, agent_concurrency=999
        )
        service._validate(request)  # 不抛即通过（CLI 可信入口语义）

        default_service = EvalRunService(evals_root=evals_root, data_root=data_root)
        with pytest.raises(InvalidSubmission):
            default_service._validate(request)

    async def test_run_sync_returns_cancelled_job_and_run_closed(self, workspace) -> None:
        """review #I01 服务侧锁定：run_sync 期间取消 → Job 与 run.json 都收口 cancelled。"""
        import asyncio as _asyncio

        evals_root, data_root = workspace
        app = create_app(
            evals_root=evals_root, data_root=data_root, fixtures_root=REPO / "fixtures"
        )
        service: EvalRunService = app.state.eval_run_service
        request = EvalRunRequest(
            agent_profile="local-fake", benchmark="database-core", tags=["smoke"], repeat=5
        )
        task = _asyncio.create_task(service.run_sync(request))
        deadline = time.monotonic() + 30
        job = None
        while time.monotonic() < deadline:
            jobs = service.list()
            if jobs and jobs[0].status is JobStatus.RUNNING:
                job = jobs[0]
                break
            await _asyncio.sleep(0.02)
        assert job is not None, "run_sync 应已进入 RUNNING"

        service.cancel(job.job_id)
        returned = await task
        assert returned.status is JobStatus.CANCELLED
        # Runner 收到真实 cancellation：run.json 收口成 cancelled
        if returned.run_id:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                run_status = (await client.get(f"/api/runs/{returned.run_id}/status")).json()
            assert run_status["status"] == "cancelled"


class TestCustomExecutorInjection:
    """§23：服务层只认 JobExecutor 协议——自定义执行器可整体替换本地实现。"""

    def test_service_accepts_protocol_executor(self, workspace) -> None:
        submitted: list[str] = []
        cancelled: list[str] = []

        class RecordingExecutor:
            def __init__(self) -> None:
                self.jobs: dict = {}

            def submit(self, job_id: str, body) -> None:
                submitted.append(job_id)

            def cancel(self, job_id: str) -> bool:
                cancelled.append(job_id)
                return True

            def is_in_flight(self, job_id: str) -> bool:
                return False

            def in_flight_ids(self) -> set[str]:
                return set()

            def ordered_in_flight(self) -> list[str]:
                return []

            def shutdown(self) -> None: ...

        evals_root, data_root = workspace
        recorder = RecordingExecutor()
        service = EvalRunService(
            evals_root=evals_root,
            data_root=data_root,
            fixtures_root=REPO / "fixtures",
            executor=recorder,  # type: ignore[arg-type]
        )
        job = service.submit(
            EvalRunRequest(agent_profile="local-fake", benchmark="database-core", tags=["smoke"])
        )
        assert submitted == [job.job_id]
        service.cancel(job.job_id)
        assert cancelled == [job.job_id]
