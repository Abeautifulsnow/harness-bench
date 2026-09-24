"""P5 REST API 测试（PRD §84）。

测试策略：先跑一次真实的 mock run 落盘（事实层），再让 API 直接读它。
这样断言的字段（verdict、stability、gate 规则、failure 分类）都是端到端真值，
而不是手搓的假 payload。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_eval.api.app import create_app
from agent_eval.runner.runner import RunConfig, Runner

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def workspace(tmp_path: Path):
    evals_root = tmp_path / "evals"
    shutil.copytree(REPO / "evals", evals_root)
    shutil.copytree(REPO / "fixtures", tmp_path / "fixtures")
    data_root = tmp_path / "data"
    data_root.mkdir()
    return evals_root, data_root


@pytest.fixture()
def client(workspace) -> TestClient:
    evals_root, data_root = workspace
    app = create_app(evals_root=evals_root, data_root=data_root)
    return TestClient(app, raise_server_exceptions=False)


async def _run(workspace, **kw) -> str:
    evals_root, data_root = workspace
    cfg = RunConfig(
        evals_root=evals_root,
        fixtures_root=data_root.parent / "fixtures",
        data_root=data_root,
        benchmark="database-core",
        agent_endpoint="fake://",
        tag_filter=["smoke"],
        **kw,
    )
    outcome = await Runner(cfg).run()
    return outcome.run_id


class TestReadOnlyContract:
    def test_no_write_routes_are_exposed(self, client: TestClient) -> None:
        """只读保证：HTTP 层不允许任何改数据的动词（PRD §84 只列了资源路径）。"""
        schema = client.get("/api/openapi.json").json()
        verbs = set()
        for path in schema["paths"].values():
            verbs.update(path.keys())
        assert verbs == {"get"}

    def test_health(self, client: TestClient) -> None:
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["runs"] == 0
        assert payload["projection"] == "missing"


class TestCatalog:
    def test_benchmarks_and_cases(self, client: TestClient) -> None:
        benchmarks = client.get("/api/benchmarks").json()
        assert [b["name"] for b in benchmarks] == ["database-core"]
        assert benchmarks[0]["cases"] > 0

        cases = client.get("/api/benchmarks/database-core/cases").json()
        assert cases, "benchmark 应解析出 case"
        assert {c["dataset_id"] for c in cases} == {"database-core"}
        assert any("tools" in c["mount_points"] for c in cases)

    def test_case_detail_returns_assertions(self, client: TestClient) -> None:
        cases = client.get("/api/cases", params={"q": "echo"}).json()
        assert cases
        detail = client.get(f"/api/cases/{cases[0]['id']}").json()
        assert detail["id"] == cases[0]["id"]
        assert "expected" in detail

    def test_unknown_benchmark_is_404(self, client: TestClient) -> None:
        assert client.get("/api/benchmarks/nope").status_code == 404

    def test_suites_include_security_kinds(self, client: TestClient) -> None:
        suites = client.get("/api/suites").json()
        assert {s["kind"] for s in suites} >= {"suite", "security", "red-team"}


class TestRuns:
    async def test_run_detail_tabs(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)

        overview = client.get(f"/api/runs/{run_id}").json()
        assert overview["verdict"] == "pass"
        assert overview["counts"]["cases"] > 0
        assert overview["baseline_mode"] == "NO_BASELINE"

        cases = client.get(f"/api/runs/{run_id}/cases").json()
        assert cases and cases[0]["case_run_ids"]
        case_id = cases[0]["case_id"]

        metrics = client.get(f"/api/runs/{run_id}/cases/{case_id}/metrics").json()
        assert metrics and any(m["metric"].startswith("native.") for m in metrics)

        case_runs = client.get(f"/api/runs/{run_id}/cases/{case_id}/runs").json()
        assert case_runs[0]["status"] in {"PASS", "FAIL", "ERROR"}

    async def test_trace_view_builds_span_tree(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        cases = client.get(f"/api/runs/{run_id}/cases").json()
        case_id = cases[0]["case_id"]

        trace = client.get(f"/api/runs/{run_id}/traces/{case_id}").json()
        assert trace["span_count"] > 0, "run 默认保存 trace，Span Tree 应可重建"
        assert trace["root"] is not None
        events = client.get(f"/api/runs/{run_id}/traces/{case_id}/events").json()
        assert events["count"] > 0
        assert all("type" in event for event in events["events"])

    async def test_artifacts_are_listed_and_readable(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        artifacts = client.get(f"/api/runs/{run_id}/artifacts").json()
        names = {a["name"] for a in artifacts}
        assert {"report.json", "gate.json", "junit.xml", "report.html", "summary.md"} <= names

        report = client.get(f"/api/runs/{run_id}/artifacts/report.json").json()
        assert report["content_type"] == "application/json"
        assert '"schema": "agent-eval/report@v2"' in report["text"]

        html = client.get(f"/api/runs/{run_id}/artifacts/report.html/raw")
        assert html.status_code == 200
        assert "text/html" in html.headers["content-type"]

    def test_unknown_run_is_404(self, client: TestClient) -> None:
        assert client.get("/api/runs/run_missing").status_code == 404

    async def test_run_list_filters(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        rows = client.get("/api/runs", params={"benchmark": "database-core"}).json()
        assert [row["run_id"] for row in rows] == [run_id]
        assert client.get("/api/runs", params={"benchmark": "other"}).json() == []


class TestQualityViews:
    async def test_gate_rules_and_replay(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        rulesets = client.get("/api/gates/rules").json()
        assert {row["gate"] for row in rulesets} >= {"pr", "main", "release"}

        gate = client.get(f"/api/gates/{run_id}").json()
        assert gate["source"] == "stored"
        assert gate["verdict"] in {"pass", "fail", "undetermined"}
        assert gate["exit_code"] == 0
        assert gate["aggregate"], "junit 计数必须来自 gate.json 的 aggregate 块"

    async def test_no_baseline_degrades_gate_rules(self, workspace, client: TestClient) -> None:
        """Spec §4.3：NO_BASELINE 时相对规则 undetermined，绝对规则照常判定。"""
        run_id = await _run(workspace)
        gate = client.get(f"/api/gates/{run_id}").json()
        verdicts = {rule["rule"]: rule["verdict"] for rule in gate["rules"]}
        assert any(v == "undetermined" for v in verdicts.values())
        assert gate["baseline_mode"] == "NO_BASELINE"

    def test_baseline_resolve_rejects_no_baseline(self, client: TestClient) -> None:
        response = client.get(
            "/api/baselines/resolve",
            params={"benchmark": "database-core", "mode": "NO_BASELINE"},
        )
        assert response.status_code == 400

    async def test_reviews_and_queue(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        options = client.get("/api/reviews/options").json()
        assert "FALSE_POSITIVE" in options["verdicts"]
        queue = client.get(f"/api/reviews/queue/{run_id}").json()
        assert queue["run_id"] == run_id
        assert client.get("/api/reviews").json()["projection"] == "ok"

    async def test_security_posture_lists_coverage(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        posture = client.get("/api/security", params={"run_id": run_id}).json()
        assert {row["category"] for row in posture["coverage"]} == set(
            posture["red_team_categories"]
        )
        assert len(posture["suites"]) == 2


class TestAnalyticsViews:
    def test_projections_report_missing_instead_of_empty(self, client: TestClient) -> None:
        """投影未构建 ≠ 没有数据：必须给可执行提示，而不是空数组。"""
        trends = client.get("/api/trends").json()
        assert trends["projection"] == "missing"
        assert "storage rebuild" in trends["hint"]
        flaky = client.get("/api/flaky").json()
        assert flaky["projection"] == "missing"

    async def test_cost_is_null_without_pricing(self, workspace, client: TestClient) -> None:
        """PRD §59：无定价时 cost 为 null，绝不伪造 0.0。"""
        await _run(workspace)
        rows = client.get("/api/cost").json()
        assert rows
        assert rows[0]["total_cost"] is None
        assert rows[0]["note"]

    async def test_dashboard_cards(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        payload = client.get("/api/dashboard").json()
        labels = {card["label"] for card in payload["cards"]}
        assert {"Task Success", "Regression Count", "Security Failures"} <= labels
        assert payload["recent_runs"][0]["run_id"] == run_id
        assert payload["current_release"] is None, "未 pin release baseline 时不应编造发布点"


class TestRegressionViews:
    async def test_regression_requires_baseline(self, workspace, client: TestClient) -> None:
        run_id = await _run(workspace)
        response = client.get(f"/api/regressions/{run_id}")
        assert response.status_code == 409
        assert "NO_BASELINE" in response.json()["error"]

    async def test_regression_between_two_runs(self, workspace, client: TestClient) -> None:
        first = await _run(workspace)
        second = await _run(workspace)
        payload = client.get(f"/api/regressions/{first}/{second}").json()
        assert payload["valid"] is True
        assert payload["baseline_run_id"] == first
        assert payload["candidate_run_id"] == second
        diffs = {row["metric"]: row["verdict"] for row in payload["metric_diffs"]}
        # 同一个 mock 跑两次：指标必须判 unchanged，而不是随方向噪声乱标
        assert diffs, "两侧 run 至少应有一个可比的 run-level metric"
        assert set(diffs.values()) == {"unchanged"}
