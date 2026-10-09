"""Production Online Eval 自动化测试（monitor 过滤/采样/tick/回填/趋势/告警）。"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import httpx
import pytest

import agent_eval.execution.notifier as notifier_mod
from agent_eval.errors import InvalidCallError
from agent_eval.execution.production_monitors import (
    ProductionMonitorDef,
    ProductionMonitorService,
    load_production_monitors,
    online_eval_trend,
)
from agent_eval.storage.production_store import ProductionStore

REPO = Path(__file__).resolve().parents[1]

MONITOR_YAML = """\
id: qa-auto
display_name: QA Auto
policy: qa
filter:
  source: platform
sampling_rate: {rate}
alert:
  metric: agent.task_completion
  max_fail_rate_percent: {threshold}
{notify}
"""


def _write_monitor(evals_root: Path, **kw) -> None:
    (evals_root / "production-monitors").mkdir(exist_ok=True)
    text = MONITOR_YAML.format(
        rate=kw.get("rate", 100),
        threshold=kw.get("threshold", 30),
        notify=kw.get("notify", ""),
    )
    (evals_root / "production-monitors" / "qa-auto.yaml").write_text(text, encoding="utf-8")


def _write_policy(evals_root: Path) -> None:
    (evals_root / "eval-policies").mkdir(exist_ok=True)
    (evals_root / "eval-policies" / "qa.yaml").write_text(
        "id: qa\n"
        "display_name: QA judge\n"
        "metrics:\n"
        "  - id: agent.task_completion\n"
        "    threshold: 0.7\n",
        encoding="utf-8",
    )


class StubAdapter:
    def __init__(self, score: float = 0.9, fail: bool = False) -> None:
        self.score = score
        self.fail = fail

    def version(self) -> str | None:
        return "stub-1.0"

    async def evaluate(self, metric_id, threshold, trace, model=None):
        if self.fail:
            raise RuntimeError("judge provider down")
        return self.score, "stub"


def _ingest(store: ProductionStore, trace_id: str, *, source: str = "platform") -> str:
    events = [
        {
            "event_id": "e1",
            "trace_id": "t1",
            "type": "run.started",
            "timestamp": "2026-10-09T10:00:00+08:00",
            "data": {"input": "帮我查订单"},
        },
        {
            "event_id": "e2",
            "trace_id": "t1",
            "type": "run.finished",
            "timestamp": "2026-10-09T10:00:02+08:00",
            "data": {"output": "订单已发货", "status": "success"},
        },
    ]
    return store.save(
        source=source,
        cases=[{"case_id": "conv-1", "events": events}],
        trace_id=trace_id,
        model="test-model",
    )


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, Path, ProductionStore]:
    evals_root = tmp_path / "evals"
    shutil.copytree(REPO / "evals", evals_root)
    shutil.rmtree(evals_root / "production-monitors", ignore_errors=True)
    shutil.rmtree(evals_root / "eval-policies", ignore_errors=True)
    data_root = tmp_path / "data"
    data_root.mkdir()
    return evals_root, data_root, ProductionStore(data_root)


class TestDefinition:
    def test_load_and_field_defaults(self, env) -> None:
        evals_root, _, _ = env
        _write_monitor(evals_root)
        monitors = load_production_monitors(evals_root)
        assert len(monitors) == 1
        m = monitors[0]
        assert m.enabled is True
        assert m.sampling_rate == 100.0
        assert m.backfill_batch == 20
        assert m.filter.source == "platform"

    def test_invalid_id_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            ProductionMonitorDef(id="../escape", display_name="x", policy="p")

    def test_missing_directory_is_empty(self, tmp_path: Path) -> None:
        assert load_production_monitors(tmp_path) == []


class TestFilterAndSampling:
    def _monitor(self, **kw) -> ProductionMonitorDef:
        return ProductionMonitorDef(id="m", display_name="M", policy="qa", **kw)

    def test_filter_source_and_model(self) -> None:
        m = self._monitor(filter={"source": "otel", "model": "gpt"})
        assert m.matches({"source": "otel", "model": "gpt"})
        assert not m.matches({"source": "platform", "model": "gpt"})
        assert not m.matches({"source": "otel", "model": None})

    def test_filter_case_prefix_requires_all_cases(self) -> None:
        m = self._monitor(filter={"case_id_prefix": "prod-"})
        assert m.matches({"cases": {"prod-a": {}, "prod-b": {}}})
        assert not m.matches({"cases": {"prod-a": {}, "conv-b": {}}})
        assert not m.matches({"cases": {}})  # 空 case 集不构成命中

    def test_sampling_is_deterministic_per_trace(self) -> None:
        m = self._monitor(sampling_rate=50)
        assert m.sampled_in("prod_x") == m.sampled_in("prod_x")
        assert len({m.sampled_in(f"prod_{i:016x}") for i in range(200)}) == 2

    def test_sampling_boundaries(self) -> None:
        assert self._monitor(sampling_rate=0).sampled_in("anything") is False
        assert self._monitor(sampling_rate=100).sampled_in("anything") is True


class TestTickAndBackfill:
    def test_tick_evaluates_new_traces_and_advances_cursor(self, env) -> None:
        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(evals_root)
        first = _ingest(store, "prod_0000000000000001")
        second = _ingest(store, "prod_0000000000000002")
        service = ProductionMonitorService(evals_root, data_root, StubAdapter())
        assert service.tick() == ["qa-auto"]
        assert len(store.list_evaluations(first)) == 1
        assert len(store.list_evaluations(second)) == 1
        record = store.list_evaluations(first)[0]
        assert record["triggered_by"] == "monitor:qa-auto"
        state = service.states.get("qa-auto")
        assert state["cursor"] == second  # 已推进到最新
        assert state["evaluated_total"] == 2
        # cursor 之后没有新 trace：再 tick 不重复产评测
        assert service.tick() == []

    def test_tick_respects_filter_and_sampling(self, env) -> None:
        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(evals_root, rate=0)  # 全部采样淘汰
        _ingest(store, "prod_0000000000000001", source="otel")  # 过滤淘汰
        _ingest(store, "prod_0000000000000002")
        service = ProductionMonitorService(evals_root, data_root, StubAdapter())
        assert service.tick() == []
        # 决策为"跳过"的 trace 同样推进 cursor（确定性决策不重看）
        assert service.states.get("qa-auto")["cursor"] == "prod_0000000000000002"
        assert store.list_evaluations("prod_0000000000000002") == []

    def test_unknown_policy_fails_into_state_not_thread(self, env) -> None:
        evals_root, data_root, store = env
        _write_monitor(evals_root)  # 不写 policy
        _ingest(store, "prod_0000000000000001")
        service = ProductionMonitorService(evals_root, data_root, StubAdapter())
        assert service.tick() == []
        assert "unknown eval policy" in service.states.get("qa-auto")["error"]

    def test_backfill_rescans_below_cursor(self, env) -> None:
        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(evals_root)
        old = _ingest(store, "prod_0000000000000001")
        service = ProductionMonitorService(evals_root, data_root, StubAdapter())
        assert service.tick() == ["qa-auto"]
        # 新 policy 上线，历史 trace 显式回填
        result = service.backfill("qa-auto")
        assert result["evaluated"] >= 1
        assert len(store.list_evaluations(old)) == 2  # 原评测 + 回填评测并存

    def test_backfill_unknown_monitor(self, env) -> None:
        evals_root, data_root, _ = env
        service = ProductionMonitorService(evals_root, data_root, StubAdapter())
        with pytest.raises(InvalidCallError):
            service.backfill("nope")


class TestAlert:
    def test_alert_fires_log_and_webhook(self, env, monkeypatch) -> None:
        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(
            evals_root,
            threshold=30,
            notify="notify:\n  webhook: http://hook\n",
        )
        _ingest(store, "prod_0000000000000001")
        monkeypatch.setattr(notifier_mod, "BACKOFF_SECONDS", (0.0, 0.0))
        calls: list[dict] = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, "json": kwargs.get("json")})

            class Resp:
                is_success = True

            return Resp()

        monkeypatch.setattr(notifier_mod.httpx, "post", fake_post)
        service = ProductionMonitorService(evals_root, data_root, StubAdapter(score=0.1))
        assert service.tick() == ["qa-auto"]
        records = [
            json.loads(line)
            for line in service.alert_log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(records) == 1
        assert records[0]["monitor"] == "qa-auto"
        assert records[0]["fail_rate_percent"] == 100.0
        assert records[0]["trace_id"] == "prod_0000000000000001"
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls and calls[0]["url"] == "http://hook"
        assert calls[0]["json"]["kind"] == "production_monitor_alert"

    def test_alert_silent_when_quality_good(self, env) -> None:
        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(evals_root, threshold=30)
        _ingest(store, "prod_0000000000000001")
        service = ProductionMonitorService(evals_root, data_root, StubAdapter(score=0.9))
        assert service.tick() == ["qa-auto"]
        assert not service.alert_log_path.exists()

    def test_error_verdicts_do_not_masquerade_as_quality_breach(self, env) -> None:
        """judge 全挂 → error 不进分母，不得触发质量告警（那是要修 judge，不是质量差）。"""
        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(evals_root, threshold=30)
        _ingest(store, "prod_0000000000000001")
        service = ProductionMonitorService(evals_root, data_root, StubAdapter(fail=True))
        assert service.tick() == ["qa-auto"]
        assert not service.alert_log_path.exists()


class TestAutoEnqueue:
    """P1-2：monitor 评测 FAIL 自动入 Review Queue（pending，人工结论不覆盖）。"""

    def test_enqueue_failures_creates_pending_entries_and_dedups_on_backfill(self, env) -> None:
        from agent_eval.review.store import ReviewStore

        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(evals_root)
        monitor_path = evals_root / "production-monitors" / "qa-auto.yaml"
        monitor_path.write_text(
            monitor_path.read_text(encoding="utf-8") + "enqueue_failures: true" + chr(10),
            encoding="utf-8",
        )
        trace_id = _ingest(store, "prod_0000000000000001")
        review = ReviewStore(data_root / "state")
        service = ProductionMonitorService(
            evals_root, data_root, StubAdapter(score=0.1), review_store=review
        )
        assert service.tick() == ["qa-auto"]
        pending = review.list(status="pending")
        assert [(r["run_id"], r["case_id"]) for r in pending] == [
            ("production:" + trace_id, "conv-1")
        ]
        assert service.states.get("qa-auto")["queued_total"] == 1
        # 回填重评同一条 trace：已有 pending，不得重复入队
        service.backfill("qa-auto")
        assert len(review.list(status="pending")) == 1
        assert service.states.get("qa-auto")["queued_total"] == 1


class TestTrend:
    def test_trend_aggregates_by_date_and_metric(self, env) -> None:
        _, _, store = env
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
        first = store.save(source="platform", cases=[{"case_id": "c", "events": events}])
        second = store.save(source="platform", cases=[{"case_id": "c", "events": events}])
        today = time.strftime("%Y-%m-%d")
        store.save_evaluation(
            first,
            {
                "evaluation_id": "eval_00000001aaaaaaaa",
                "rows": [
                    {"metric": "agent.task_completion", "verdict": "pass", "score": 0.9},
                    {"metric": "agent.task_completion", "verdict": "fail", "score": 0.2},
                    {"metric": "agent.task_completion", "verdict": "error", "score": None},
                ],
                "summary": {},
            },
        )
        store.save_evaluation(
            second,
            {
                "evaluation_id": "eval_00000002aaaaaaaa",
                "rows": [{"metric": "agent.task_completion", "verdict": "pass", "score": 0.8}],
                "summary": {},
            },
        )
        trend = online_eval_trend(store, days=30)
        assert len(trend) == 1
        bucket = trend[0]
        assert bucket["date"] == today
        assert bucket["metric"] == "agent.task_completion"
        assert bucket["pass"] == 2 and bucket["fail"] == 1 and bucket["error"] == 1
        assert bucket["mean_score"] == round((0.9 + 0.2 + 0.8) / 3, 4)


class TestApi:
    @staticmethod
    def _client(env, evaluator) -> httpx.AsyncClient:
        import httpx as _httpx

        from agent_eval.api.app import create_app

        evals_root, data_root, _ = env
        app = create_app(
            evals_root=evals_root,
            data_root=data_root,
            fixtures_root=REPO / "fixtures",
            production_evaluator=evaluator,
        )
        return _httpx.AsyncClient(transport=_httpx.ASGITransport(app=app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_monitor_views_backfill_and_trends(self, env) -> None:
        evals_root, data_root, store = env
        _write_policy(evals_root)
        _write_monitor(evals_root)
        trace_id = _ingest(store, "prod_0000000000000001")
        async with self._client(env, StubAdapter()) as client:
            views = (await client.get("/api/production/monitors")).json()
            assert [v["id"] for v in views] == ["qa-auto"]
            assert views[0]["policy"] == "qa"
            assert views[0]["alert_metric"] == "agent.task_completion"

            backfill = await client.post("/api/production/monitors/qa-auto/backfill")
            assert backfill.status_code == 200, backfill.text
            assert backfill.json()["evaluated"] == 1

            unknown = await client.post("/api/production/monitors/nope/backfill")
            assert unknown.status_code == 404

            trends = (await client.get("/api/production/trends")).json()
            assert trends and trends[0]["pass"] == 1
            assert (await client.get(f"/api/production/traces/{trace_id}/evaluations")).json()

    @pytest.mark.asyncio
    async def test_backfill_is_token_gated(self, env, monkeypatch) -> None:
        evals_root, data_root, _ = env
        _write_policy(evals_root)
        _write_monitor(evals_root)
        monkeypatch.setenv("AGENT_EVAL_EXEC_TOKEN", "s3cret")
        async with self._client(env, StubAdapter()) as client:
            denied = await client.post("/api/production/monitors/qa-auto/backfill")
            assert denied.status_code in (401, 403)
            allowed = await client.post(
                "/api/production/monitors/qa-auto/backfill",
                headers={"Authorization": "Bearer s3cret"},
            )
            assert allowed.status_code == 200
