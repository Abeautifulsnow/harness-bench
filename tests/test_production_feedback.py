"""P1-2 生产反馈闭环测试：失败入队（pending）→ 人工结论 → promote draft。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent_eval.api.app import create_app
from agent_eval.errors import InvalidCallError
from agent_eval.evaluators.online_eval import EvalPair
from agent_eval.failures.promote import (
    build_draft_from_production,
    validate_assertions,
)
from agent_eval.review.production_queue import queue_production_failures
from agent_eval.review.store import ReviewStore

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[ReviewStore, Path]:
    state_root = tmp_path / "state"
    return ReviewStore(state_root), tmp_path


class TestReviewPendingSemantics:
    def test_pending_entry_via_empty_verdict_with_reason(self, stores) -> None:
        review, _ = stores
        record = review.add("production:t1", "c1", "", queue_reason="regression")
        assert record["verdict"] == ""
        pending = review.list(status="pending")
        assert [(r["run_id"], r["case_id"]) for r in pending] == [("production:t1", "c1")]

    def test_empty_verdict_requires_reason(self, stores) -> None:
        review, _ = stores
        with pytest.raises(InvalidCallError):
            review.add("production:t1", "c1", "")

    def test_machine_verdict_persisted_and_bad_verdict_still_rejected(self, stores) -> None:
        review, _ = stores
        record = review.add(
            "production:t1", "c1", "", queue_reason="regression", machine_verdict="FAIL"
        )
        assert record["machine_verdict"] == "FAIL"
        with pytest.raises(InvalidCallError):
            review.add("r", "c", "MAYBE")  # 五值之外依旧拒绝
        with pytest.raises(InvalidCallError):
            review.add("r", "c", "EXPECTED")  # note 必填契约不变

    def test_human_verdict_supersedes_pending(self, stores) -> None:
        review, _ = stores
        review.add("production:t1", "c1", "", queue_reason="regression")
        review.add("production:t1", "c1", "EXPECTED", note="回答正确，误报")
        assert review.list(status="pending") == []
        effective = review.effective(run_id="production:t1")[("production:t1", "c1")]
        assert effective["verdict"] == "EXPECTED"


class TestQueueProductionFailures:
    @staticmethod
    def _evaluation() -> dict:
        return {
            "evaluation_id": "eval_00000001aaaaaaaa",
            "rows": [
                {"case_id": "c1", "metric": "m", "verdict": "fail", "score": 0.2},
                {"case_id": "c2", "metric": "m", "verdict": "pass", "score": 0.9},
                {"case_id": "c3", "metric": "m", "verdict": "error", "score": None},
            ],
            "summary": {},
        }

    def test_enqueue_only_fail_rows(self, stores) -> None:
        review, _ = stores
        enqueued = queue_production_failures(review, trace_id="t1", evaluation=self._evaluation())
        assert [r["case_id"] for r in enqueued] == ["c1"]
        assert enqueued[0]["queue_reason"] == "regression"
        assert enqueued[0]["machine_verdict"] == "FAIL"
        assert "eval_00000001aaaaaaaa" in enqueued[0]["note"]

    def test_dedup_pending_but_reenqueu_after_human_verdict_is_still_skipped(self, stores) -> None:
        review, _ = stores
        queue_production_failures(review, trace_id="t1", evaluation=self._evaluation())
        again = queue_production_failures(review, trace_id="t1", evaluation=self._evaluation())
        assert again == [], "已有 pending 条目不得重复入队"
        # 人工已判 EXPECTED：机器再次 FAIL 也不得重新入队（人工结论不覆盖）
        review.add("production:t1", "c1", "EXPECTED", note="误报")
        third = queue_production_failures(review, trace_id="t1", evaluation=self._evaluation())
        assert third == []


class TestPromoteFromProduction:
    def test_draft_fields_and_assertions_are_schema_valid(self, stores) -> None:
        _, tmp = stores
        pair = EvalPair(
            case_id="conv-9",
            input="帮我查订单",
            actual_output="查不到",
            tools_called=["order.search", "order.search"],
        )
        draft = build_draft_from_production(
            "prod_abc",
            pair,
            evaluation_id="eval_1",
            failure_reason="score 0.2 below threshold",
        )
        assert draft.source_type == "production"
        assert draft.source_ref == "prod_abc"
        assert draft.run_id == "production:prod_abc"
        assert draft.suite == "regression"
        assert draft.inputs == {"type": "single_turn", "prompt": "帮我查订单"}
        assert draft.tool_expectations["required"] == ["order.search"]
        assert validate_assertions(draft.suggested_assertions) == []
        # Case Draft 的 YAML 形态可落盘待评审（人工补 output 断言后进回归集）
        out = tmp / "cases"
        path = draft_store_roundtrip(draft, out)
        text = path.read_text(encoding="utf-8")
        assert "promoted.prod_abc.conv-9" in text
        assert "source:" in text and "production" in text


def draft_store_roundtrip(draft, out: Path) -> Path:
    from agent_eval.failures.promote import DraftStore

    store = DraftStore(out.parent / "state")
    store.save(draft)
    return store.to_case_yaml(draft.id, out)


class TestApi:
    @staticmethod
    def _client(tmp_path: Path) -> httpx.AsyncClient:
        app = create_app(
            evals_root=REPO / "evals",
            data_root=tmp_path / "data",
            fixtures_root=REPO / "fixtures",
        )

        class StubAdapter:
            def version(self) -> str | None:
                return "stub-1.0"

            async def evaluate(self, metric_id, threshold, trace, model=None):
                return 0.1, "bad answer"  # 稳定 FAIL

        app.state.production_evaluator = StubAdapter()
        app.state.production_monitors.evaluator = app.state.production_evaluator
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def _ingest_and_evaluate(self, client: httpx.AsyncClient) -> tuple[str, str]:
        events = [
            {
                "event_id": "e1",
                "trace_id": "t1",
                "type": "run.started",
                "timestamp": "2026-10-09T10:00:00+08:00",
                "data": {"input": "help me with ops"},
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
                "data": {"output": "sorry cannot", "status": "success"},
            },
        ]
        trace_id = (
            await client.post(
                "/api/production/traces",
                json={"source": "platform", "cases": [{"case_id": "conv-9", "events": events}]},
            )
        ).json()["trace_id"]
        evaluation = await client.post(f"/api/production/traces/{trace_id}/evaluate", json={})
        assert evaluation.status_code in (201, 400), evaluation.text
        # evals/ 自带 production-qa policy；POST body 缺 policy 会 400，显式带上
        if evaluation.status_code == 400:
            evaluation = await client.post(
                f"/api/production/traces/{trace_id}/evaluate",
                json={"policy": "production-qa"},
            )
        assert evaluation.status_code == 201, evaluation.text
        return trace_id, evaluation.json()["evaluation_id"]

    @pytest.mark.asyncio
    async def test_review_queue_and_promote_flow(self, tmp_path: Path) -> None:
        async with self._client(tmp_path) as client:
            trace_id, evaluation_id = await self._ingest_and_evaluate(client)

            queued = await client.post(
                f"/api/production/traces/{trace_id}/review-queue",
                json={"evaluation_id": evaluation_id},
            )
            assert queued.status_code == 200, queued.text
            assert [r["case_id"] for r in queued.json()["enqueued"]] == ["conv-9"]
            # 再入队：pending 去重
            again = await client.post(
                f"/api/production/traces/{trace_id}/review-queue",
                json={"evaluation_id": evaluation_id},
            )
            assert again.json()["enqueued"] == []

            promoted = await client.post(
                f"/api/production/traces/{trace_id}/promote",
                json={"case_id": "conv-9", "evaluation_id": evaluation_id},
            )
            assert promoted.status_code == 200, promoted.text
            draft = promoted.json()
            assert draft["source_type"] == "production"
            assert draft["source_ref"] == trace_id
            assert draft["tool_expectations"]["required"] == ["order.search"]

            # draft 已落 DraftStore，可从 failures 既有面读到
            drafts = (await client.get("/api/drafts")).json()
            assert any(d["id"] == draft["id"] for d in drafts)

    @pytest.mark.asyncio
    async def test_review_queue_without_evaluation_is_422(self, tmp_path: Path) -> None:
        async with self._client(tmp_path) as client:
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
            response = await client.post(f"/api/production/traces/{trace_id}/review-queue")
            assert response.status_code == 422
            promote = await client.post(
                f"/api/production/traces/{trace_id}/promote", json={"case_id": "c"}
            )
            assert promote.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_trace_is_404(self, tmp_path: Path) -> None:
        async with self._client(tmp_path) as client:
            assert (
                await client.post("/api/production/traces/prod_none/review-queue")
            ).status_code == 404
            assert (
                await client.post("/api/production/traces/prod_none/promote", json={"case_id": "c"})
            ).status_code == 404
