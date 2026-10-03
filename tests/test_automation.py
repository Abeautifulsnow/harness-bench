"""V2 Automation 测试（docs §52/§58）：Scheduled Evaluation / Preset / Trigger /
Notification / 执行 token 门。

与 test_eval_runs_api.py 同策略：fake:// agent 全链路，断言端到端真值。
调度器用注入时钟直接调 tick()，不起真实线程。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agent_eval.api.app import create_app
from agent_eval.execution.cron import CronSpec
from agent_eval.execution.models import EvalRunRequest
from agent_eval.execution.notifier import job_payload
from agent_eval.execution.presets import PresetRequest, resolve_request
from agent_eval.execution.repository import JobRepository
from agent_eval.execution.scheduler import SchedulerService

REPO = Path(__file__).resolve().parents[1]

LOCAL_AGENT = """\
id: local-fake
display_name: Local Fake Agent
endpoint: fake://
enabled: true
"""

SCHEDULE = """\
id: tick-database
display_name: Tick database
enabled: {enabled}
cron: "* * * * *"
request:
  benchmark: database-core
  agent_profile: local-fake
  tags: [smoke]
"""

SCHEDULE_WITH_PRESET = """\
id: preset-tick
display_name: Preset tick
enabled: true
cron: "* * * * *"
preset: smoke-preset
notify:
  webhook: http://127.0.0.1:9/hook
"""

PRESET = """\
id: smoke-preset
display_name: Smoke preset
request:
  benchmark: database-core
  agent_profile: local-fake
  tags: [smoke]
  repeat: 1
  no_judge: true
"""

TRIGGER = """\
id: ci-database
display_name: CI database
enabled: {enabled}
preset: smoke-preset
token_secret_ref: AUTOMATION_TEST_TRIGGER_TOKEN
"""

TRIGGER_INLINE = """\
id: inline-trigger
display_name: Inline trigger
enabled: true
request:
  benchmark: database-core
  agent_profile: local-fake
"""


@pytest.fixture()
def workspace(tmp_path: Path):
    evals_root = tmp_path / "evals"
    shutil.copytree(REPO / "evals", evals_root)
    shutil.copytree(REPO / "fixtures", tmp_path / "fixtures")
    # 仓库自带的示例资产不参与本套件（enabled: false / .example 不装载），
    # 测试用例自写可控定义。
    for name in ("schedules", "presets"):
        shutil.rmtree(evals_root / name, ignore_errors=True)
    (evals_root / "agents" / "local-fake.yaml").write_text(LOCAL_AGENT, encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()
    return evals_root, data_root


def _make_app(workspace, **kw):
    evals_root, data_root = workspace
    return create_app(
        evals_root=evals_root,
        data_root=data_root,
        fixtures_root=REPO / "fixtures",
        **kw,
    )


async def _wait_terminal(client: httpx.AsyncClient, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = (await client.get(f"/api/eval-runs/{job_id}")).json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {payload}")


class TestCron:
    def test_next_after_daily_boundary(self) -> None:
        spec = CronSpec.parse("0 3 * * *")
        assert spec.next_after(datetime(2026, 10, 2, 3, 0)) == datetime(2026, 10, 3, 3, 0)
        assert spec.next_after(datetime(2026, 10, 2, 2, 59)) == datetime(2026, 10, 2, 3, 0)

    def test_step_field(self) -> None:
        spec = CronSpec.parse("*/15 * * * *")
        assert spec.next_after(datetime(2026, 10, 2, 10, 7)) == datetime(2026, 10, 2, 10, 15)

    def test_dow_sunday_alignment(self) -> None:
        # cron 的 0 = 周日；2026-10-03 是周六 → 下一个触发是 10-04（周日）
        spec = CronSpec.parse("0 0 * * 0")
        assert spec.next_after(datetime(2026, 10, 3, 12, 0)) == datetime(2026, 10, 4, 0, 0)

    def test_dom_dow_or_rule(self) -> None:
        # Vixie 规则：13 号或周五（5）都触发。2026-10-02 是周五、10-03 是周六
        spec = CronSpec.parse("0 0 13 * 5")
        assert spec.next_after(datetime(2026, 10, 1, 0, 0)) == datetime(2026, 10, 2, 0, 0)  # 周五
        # 从周六起：下一个命中是周五 10-09（早于 13 号周二）
        assert spec.next_after(datetime(2026, 10, 3, 0, 0)) == datetime(2026, 10, 9, 0, 0)

    @pytest.mark.parametrize(
        "expr", ["* * *", "61 * * * *", "* * 0 * *", "*/0 * * * *", "5-2 * * * *", "a * * * *"]
    )
    def test_invalid_expressions(self, expr: str) -> None:
        with pytest.raises(ValueError):
            CronSpec.parse(expr)


class TestScheduler:
    # tick 用例的基准时刻必须秒位归零：否则 now.second > 50 时 next_after
    # 会落在 10 秒内，"+10s 不触发"的断言变成时间依赖 flake。
    BASE = datetime.now().astimezone().replace(second=0, microsecond=0)

    def _make(self, workspace, schedule_yaml: str, filename="tick.yaml", **kw):
        evals_root, data_root = workspace
        (evals_root / "schedules").mkdir(exist_ok=True)
        (evals_root / "schedules" / filename).write_text(schedule_yaml, encoding="utf-8")
        app = _make_app(workspace, **kw)
        service = app.state.eval_run_service
        scheduler = SchedulerService(evals_root=evals_root, data_root=data_root, service=service)
        return app, service, scheduler

    async def test_due_schedule_submits_and_records_state(self, workspace) -> None:
        app, service, scheduler = self._make(workspace, SCHEDULE.format(enabled="true"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            now = self.BASE
            # 首次见到调度只注册不触发（避免部署即连发历史）
            assert scheduler.tick(now) == []
            assert scheduler.tick(now + timedelta(seconds=10)) == []
            # "*/1 分钟"调度：跨过触发点后恰好一发
            assert scheduler.tick(now + timedelta(seconds=90)) == ["tick-database"]
            # 触发后同一窗口内不重复
            assert scheduler.tick(now + timedelta(seconds=95)) == []

            jobs = service.list()
            assert len(jobs) == 1
            assert jobs[0].requested_by == "schedule:tick-database"

            await _wait_terminal(client, jobs[0].job_id)

            views = {v.id: v for v in scheduler.schedule_views()}
            assert views["tick-database"].last_job_id == jobs[0].job_id
            assert views["tick-database"].next_run_at is not None
            assert views["tick-database"].error is None

    async def test_missed_window_coalesces_to_single_run(self, workspace) -> None:
        """进程停机跨过触发点 → 恢复后合并为一次，绝不回填历史。"""
        app, service, scheduler = self._make(workspace, SCHEDULE.format(enabled="true"))
        now = self.BASE
        scheduler.tick(now)  # 注册
        # 模拟停机 3 小时后恢复：一次 tick 只补一发（注册 tick 不产生 Job）
        later = now + timedelta(hours=3)
        assert scheduler.tick(later) == ["tick-database"]
        # 下一次触发点在补跑后 1 分钟：30 秒后的 tick 不应再发
        assert scheduler.tick(later + timedelta(seconds=30)) == []
        assert len(service.list()) == 1

    async def test_disabled_schedule_never_fires(self, workspace) -> None:
        _, service, scheduler = self._make(workspace, SCHEDULE.format(enabled="false"))
        assert scheduler.tick(datetime.now().astimezone()) == []
        assert service.list() == []

    async def test_preset_and_notify_are_propagated(self, workspace, monkeypatch) -> None:
        deliveries: list[tuple[str, dict]] = []
        import agent_eval.execution.notifier as notifier_mod

        def fake_post(url, json=None, headers=None, timeout=None):
            deliveries.append((url, json))

            class _Resp:
                is_success = True
                status_code = 200

            return _Resp()

        monkeypatch.setattr(notifier_mod.httpx, "post", fake_post)
        evals_root = workspace[0]
        (evals_root / "presets").mkdir(exist_ok=True)
        (evals_root / "presets" / "smoke-preset.yaml").write_text(PRESET, encoding="utf-8")

        app, service, scheduler = self._make(workspace, SCHEDULE_WITH_PRESET)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            now = self.BASE
            scheduler.tick(now)  # 注册
            assert scheduler.tick(now + timedelta(seconds=90)) == ["preset-tick"]
            job = service.list()[0]
            # 预设解析进了请求
            assert job.request.benchmark == "database-core"
            assert job.request.no_judge is True
            assert job.notify_webhook == "http://127.0.0.1:9/hook"

            await _wait_terminal(client, job.job_id)
            expected = job_payload(service.get(job.job_id))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not deliveries:
                await asyncio.sleep(0.05)
            assert deliveries, "终态通知必须发出"
            url, body = deliveries[0]
            assert url == "http://127.0.0.1:9/hook"
            assert body == expected

    async def test_unknown_preset_records_error_not_crash(self, workspace) -> None:
        broken = SCHEDULE_WITH_PRESET.replace("preset: smoke-preset", "preset: missing-preset")
        _, service, scheduler = self._make(workspace, broken)
        now = self.BASE
        scheduler.tick(now)  # 注册
        # 时间上触发（tick 报告），但提交失败 → 错误记进调度台账，不产生 Job
        assert scheduler.tick(now + timedelta(seconds=90)) == ["preset-tick"]
        views = {v.id: v for v in scheduler.schedule_views()}
        assert "unknown preset" in (views["preset-tick"].error or "")
        assert service.list() == []


class TestPresetResolution:
    def test_override_wins_over_preset(self) -> None:
        base = PresetRequest(repeat=3, tags=["smoke"])
        override = PresetRequest(repeat=1)
        merged = resolve_request(base, override)
        assert merged["repeat"] == 1
        assert merged["tags"] == ["smoke"]

    def test_missing_required_fields_surface_from_request_model(self) -> None:
        with pytest.raises(Exception, match="agent_profile"):
            EvalRunRequest.model_validate(resolve_request(PresetRequest(benchmark="x")))


class TestTriggersAPI:
    @pytest.fixture()
    def trigger_app(self, workspace, monkeypatch):
        evals_root, _ = workspace
        (evals_root / "presets").mkdir(exist_ok=True)
        (evals_root / "presets" / "smoke-preset.yaml").write_text(PRESET, encoding="utf-8")
        (evals_root / "triggers").mkdir(exist_ok=True)
        (evals_root / "triggers" / "ci-database.yaml").write_text(
            TRIGGER.format(enabled="true"), encoding="utf-8"
        )
        monkeypatch.setenv("AUTOMATION_TEST_TRIGGER_TOKEN", "s3cret-token")
        return _make_app(workspace)

    @pytest.mark.asyncio
    async def test_trigger_run_with_valid_token(self, workspace, trigger_app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=trigger_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/triggers/ci-database/run",
                headers={"Authorization": "Bearer s3cret-token"},
            )
            assert response.status_code == 202, response.text
            job = response.json()
            assert job["requested_by"] == "trigger:ci-database"
            assert job["request"]["benchmark"] == "database-core"

            await _wait_terminal(client, job["job_id"])

    @pytest.mark.asyncio
    async def test_trigger_rejects_bad_or_missing_token(self, trigger_app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=trigger_app), base_url="http://test"
        ) as client:
            assert (await client.post("/api/triggers/ci-database/run")).status_code == 401
            bad = await client.post(
                "/api/triggers/ci-database/run", headers={"Authorization": "Bearer wrong"}
            )
            assert bad.status_code == 401

    @pytest.mark.asyncio
    async def test_trigger_token_missing_from_env_is_400(self, workspace, monkeypatch) -> None:
        evals_root, _ = workspace
        (evals_root / "presets").mkdir(exist_ok=True)
        (evals_root / "presets" / "smoke-preset.yaml").write_text(PRESET, encoding="utf-8")
        (evals_root / "triggers").mkdir(exist_ok=True)
        (evals_root / "triggers" / "ci-database.yaml").write_text(
            TRIGGER.format(enabled="true"), encoding="utf-8"
        )
        monkeypatch.delenv("AUTOMATION_TEST_TRIGGER_TOKEN", raising=False)
        app = _make_app(workspace)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/triggers/ci-database/run")
            assert response.status_code == 400
            assert "AUTOMATION_TEST_TRIGGER_TOKEN" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_trigger_list_hides_token_value(self, trigger_app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=trigger_app), base_url="http://test"
        ) as client:
            rows = (await client.get("/api/triggers")).json()
            row = next(r for r in rows if r["id"] == "ci-database")
            assert row["token_ref"] == "AUTOMATION_TEST_TRIGGER_TOKEN"
            assert row["token_state"] == "configured"
            assert "s3cret-token" not in __import__("json").dumps(rows)

    @pytest.mark.asyncio
    async def test_trigger_idempotent_replay(self, trigger_app) -> None:
        headers = {"Authorization": "Bearer s3cret-token", "Idempotency-Key": "ci-run-1"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=trigger_app), base_url="http://test"
        ) as client:
            first = await client.post("/api/triggers/ci-database/run", headers=headers)
            second = await client.post("/api/triggers/ci-database/run", headers=headers)
            assert first.status_code == 202
            assert second.status_code == 200
            assert first.json()["job_id"] == second.json()["job_id"]


class TestExecTokenGate:
    @pytest.mark.asyncio
    async def test_post_blocked_when_env_token_set(self, workspace, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_EVAL_EXEC_TOKEN", "gate-token")
        app = _make_app(workspace)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {"agent_profile": "local-fake", "benchmark": "database-core", "tags": ["smoke"]}
            assert (await client.post("/api/eval-runs", json=body)).status_code == 401
            ok = await client.post(
                "/api/eval-runs",
                json=body,
                headers={"Authorization": "Bearer gate-token"},
            )
            assert ok.status_code == 202
            # GET 不受执行门约束
            assert (await client.get("/api/eval-runs")).status_code == 200

    @pytest.mark.asyncio
    async def test_open_when_env_unset(self, workspace, monkeypatch) -> None:
        monkeypatch.delenv("AGENT_EVAL_EXEC_TOKEN", raising=False)
        app = _make_app(workspace)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {"agent_profile": "local-fake", "benchmark": "database-core", "tags": ["smoke"]}
            response = await client.post("/api/eval-runs", json=body)
            assert response.status_code == 202


class TestSchedulesAPI:
    @pytest.mark.asyncio
    async def test_schedules_list_view(self, workspace) -> None:
        evals_root, _ = workspace
        (evals_root / "schedules").mkdir(exist_ok=True)
        (evals_root / "schedules" / "tick.yaml").write_text(SCHEDULE.format(enabled="true"))
        app = _make_app(workspace)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            rows = (await client.get("/api/schedules")).json()
            assert rows[0]["id"] == "tick-database"
            assert rows[0]["cron"] == "* * * * *"
            assert rows[0]["enabled"] is True
            assert rows[0]["last_job_id"] is None


class TestRestartReapWithRepo:
    @pytest.mark.asyncio
    async def test_persisted_jobs_survive_service_recreation(self, workspace) -> None:
        """Job 账本持久化：重启（新建 service）后历史可读，孤儿被收口。"""
        app = _make_app(workspace)
        service = app.state.eval_run_service
        job = service.submit(
            EvalRunRequest(agent_profile="local-fake", benchmark="database-core", tags=["smoke"])
        )
        await asyncio.sleep(0.1)
        repo = JobRepository(workspace[1] / "jobs")
        assert repo.get(job.job_id) is not None
