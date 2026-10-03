"""§59 Notification 升级测试：webhook 重试/退避 + 站内通知 feed。"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest

import agent_eval.execution.notifier as notifier_mod
from agent_eval.api.app import create_app
from agent_eval.execution.models import EvalRunJob, EvalRunRequest, JobStatus
from agent_eval.execution.notifications import NotificationStore, in_app_feed
from agent_eval.execution.notifier import BACKOFF_SECONDS, job_payload

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


def _job(notify: str | None = None) -> EvalRunJob:
    return EvalRunJob(
        job_id="job_nt",
        status=JobStatus.SUCCEEDED,
        request=EvalRunRequest(agent_profile="local-fake", benchmark="database-core"),
        run_id="run_nt",
        gate="pr",
        gate_verdict="pass",
        notify_webhook=notify,
        finished_at=None,
    )


class TestWebhookRetry:
    @pytest.fixture(autouse=True)
    def _no_backoff_wait(self, monkeypatch):
        monkeypatch.setattr(notifier_mod, "BACKOFF_SECONDS", (0.0, 0.0))

    def test_retries_on_5xx_then_succeeds(self, tmp_path, monkeypatch) -> None:
        calls: list[int] = []

        def flaky(url, **kwargs):
            calls.append(1)
            status = 500 if len(calls) < 3 else 200

            class Resp:
                is_success = status == 200
                status_code = status

            return Resp()

        monkeypatch.setattr(notifier_mod.httpx, "post", flaky)
        log = tmp_path / "notifications.jsonl"
        notifier_mod._deliver("http://hook", job_payload(_job("http://hook")), {}, log)

        assert len(calls) == 3, "5xx 是暂态失败，应重试至成功"
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert [r["attempt"] for r in records] == [1, 2, 3]
        assert records[-1]["outcome"] == "delivered"

    def test_no_retry_on_4xx(self, tmp_path, monkeypatch) -> None:
        calls: list[int] = []

        def permanent(url, **kwargs):
            calls.append(1)

            class Resp:
                is_success = False
                status_code = 404

            return Resp()

        monkeypatch.setattr(notifier_mod.httpx, "post", permanent)
        log = tmp_path / "notifications.jsonl"
        notifier_mod._deliver("http://hook", job_payload(_job("http://hook")), {}, log)

        assert len(calls) == 1, "4xx 是永久性配置问题，重试只会刷屏"
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert records[0]["outcome"] == "HTTP 404"

    def test_network_error_retries_then_gives_up(self, tmp_path, monkeypatch) -> None:
        calls: list[int] = []

        def down(url, **kwargs):
            calls.append(1)
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(notifier_mod.httpx, "post", down)
        log = tmp_path / "notifications.jsonl"
        notifier_mod._deliver("http://hook", job_payload(_job("http://hook")), {}, log)

        assert len(calls) == notifier_mod.MAX_ATTEMPTS
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert all("error" in r["outcome"] for r in records)

    def test_backoff_schedule_shape(self) -> None:
        assert notifier_mod.MAX_ATTEMPTS == 3
        assert len(BACKOFF_SECONDS) >= notifier_mod.MAX_ATTEMPTS - 1
        assert BACKOFF_SECONDS[1] > BACKOFF_SECONDS[0], "退避必须递增"


class TestInAppFeed:
    def test_feed_projects_terminal_jobs_only(self, tmp_path) -> None:
        store = NotificationStore(tmp_path)
        jobs = [
            EvalRunJob(
                job_id="job_run",
                status=JobStatus.RUNNING,
                request=EvalRunRequest(agent_profile="a", benchmark="b"),
            ),
            EvalRunJob(
                job_id="job_ok",
                status=JobStatus.SUCCEEDED,
                request=EvalRunRequest(agent_profile="a", benchmark="b"),
                run_id="run_1",
                gate_verdict="pass",
            ),
            EvalRunJob(
                job_id="job_bad",
                status=JobStatus.FAILED,
                request=EvalRunRequest(agent_profile="a", benchmark="b"),
                error="boom",
            ),
        ]
        feed = in_app_feed(jobs, store)
        assert [item["job_id"] for item in feed] == ["job_ok", "job_bad"]
        assert all(item["read"] is False for item in feed)

    def test_mark_read_roundtrip_and_persistence(self, tmp_path) -> None:
        store = NotificationStore(tmp_path)
        store.mark_read(["job_a", "job_b"])
        # 新实例（模拟重启）仍能读到
        assert NotificationStore(tmp_path).read_job_ids() == {"job_a", "job_b"}

        jobs = [
            EvalRunJob(
                job_id="job_a",
                status=JobStatus.SUCCEEDED,
                request=EvalRunRequest(agent_profile="a", benchmark="b"),
            )
        ]
        feed = in_app_feed(jobs, store)
        assert feed[0]["read"] is True

    def test_read_marks_capped(self, tmp_path) -> None:
        store = NotificationStore(tmp_path)
        store.mark_read([f"job_{index:04d}" for index in range(600)])
        assert len(store.read_job_ids()) == 500

    def test_feed_limit(self, tmp_path) -> None:
        store = NotificationStore(tmp_path)
        jobs = [
            EvalRunJob(
                job_id=f"job_{index:03d}",
                status=JobStatus.CANCELLED,
                request=EvalRunRequest(agent_profile="a", benchmark="b"),
            )
            for index in range(10)
        ]
        assert len(in_app_feed(jobs, store, limit=3)) == 3


class TestNotificationsAPI:
    @staticmethod
    def _client(workspace):
        evals_root, data_root = workspace

        app = create_app(
            evals_root=evals_root, data_root=data_root, fixtures_root=REPO / "fixtures"
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def _wait_terminal(self, client, job_id: str) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            payload = (await client.get(f"/api/eval-runs/{job_id}")).json()
            if payload["status"] in {"succeeded", "failed", "cancelled"}:
                return
            await asyncio.sleep(0.2)
        raise AssertionError("job did not finish")

    @pytest.mark.asyncio
    async def test_feed_and_mark_read_over_http(self, workspace) -> None:
        async with self._client(workspace) as client:
            body = {"agent_profile": "local-fake", "benchmark": "database-core", "tags": ["smoke"]}
            job = (await client.post("/api/eval-runs", json=body)).json()
            await self._wait_terminal(client, job["job_id"])

            feed = (await client.get("/api/notifications")).json()
            assert feed, "终态 Job 必须出现在站内通知里"
            assert feed[0]["job_id"] == job["job_id"]
            assert feed[0]["read"] is False

            marked = await client.post("/api/notifications/read", json={"job_ids": [job["job_id"]]})
            assert marked.status_code == 200
            feed = (await client.get("/api/notifications")).json()
            assert feed[0]["read"] is True

    @pytest.mark.asyncio
    async def test_mark_read_validates_body(self, workspace) -> None:
        async with self._client(workspace) as client:
            bad = await client.post("/api/notifications/read", json={"job_ids": "not-a-list"})
            assert bad.status_code == 400
