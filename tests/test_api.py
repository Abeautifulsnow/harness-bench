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
    def test_write_routes_are_execution_only(self, client: TestClient) -> None:
        """Execution 边界（docs/web-evaluation-control-plane-design.md §42 + §58/§59）：

        ``Definition mutation verbs = forbidden``；
        ``Execution mutation verbs = allowed``（eval-runs 发起/取消、triggers 触发），
        另有两类辅助动词：**UI 状态**（notifications/read 已读标记）与
        **生产摄取**（production/traces，token 门，§60.2）——都不是 Definition。
        PUT/DELETE/PATCH 任何资源都不存在。
        """
        schema = client.get("/api/openapi.json").json()
        for path, operations in schema["paths"].items():
            for verb in operations:
                assert verb in {"get", "post"}, f"unexpected verb {verb!r} on {path}"
                if verb == "post":
                    allowed = (
                        "/api/eval-runs",
                        "/api/triggers",
                        "/api/notifications/read",
                        "/api/production",
                    )
                    assert path.startswith(allowed), f"unexpected POST path: {verb.upper()} {path}"

    def test_definition_mutation_verbs_are_rejected(self, client: TestClient) -> None:
        """对 Definition 资源发写入动词必须被路由层拒绝（405：不存在该动词的端点）。"""
        assert client.post("/api/benchmarks", json={}).status_code == 405
        assert client.put("/api/gates", json={}).status_code == 405
        assert client.delete("/api/cases/database-core").status_code == 405
        assert client.patch("/api/runs/run_x").status_code == 405

    def test_health(self, client: TestClient) -> None:
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["runs"] == 0
        assert payload["projection"] == "missing"


class TestCatalog:
    def test_benchmarks_and_cases(self, client: TestClient) -> None:
        benchmarks = client.get("/api/benchmarks").json()
        # 这个端点是"列目录"，不是"列某一个 benchmark"：写死名单会让目录接口
        # 每加一个 DataSet 就假红一次，而它本该自动收录新定义。
        names = [b["name"] for b in benchmarks]
        assert "database-core" in names, names
        assert names == sorted(names), f"benchmark 列表应当有序：{names}"
        assert all(b["cases"] > 0 for b in benchmarks), names

        cases = client.get("/api/benchmarks/database-core/cases").json()
        assert cases, "benchmark 应解析出 case"
        assert {c["dataset_id"] for c in cases} == {"database-core"}
        assert any("tools" in c["mount_points"] for c in cases)

    def test_every_benchmark_resolves_its_own_cases(self, client: TestClient) -> None:
        """逐个 benchmark 校验，不是只校一个：目录里有一个坏的，其余仍会显示正常。

        `cases` 的口径是**套件选中**的条数（与 `/cases` 端点同一实现）：不等于
        dataset 条数的情形真实存在（database-core 40 → 24），而"数得到却跑不到"
        的 case 必须在这两个端点上给出同一个数，否则缺口会在列表页被藏起来。
        """
        for bench in client.get("/api/benchmarks").json():
            cases = client.get(f"/api/benchmarks/{bench['name']}/cases").json()
            assert cases, f"{bench['name']} 解析不出 case"
            assert len(cases) == bench["cases"], f"{bench['name']} 计数与解析结果不一致"
            # `dataset` 是 "<id>@<version>" 引用，case 行给的是 <id>：比对前半段
            expected_id = str(bench["dataset"]).partition("@")[0]
            assert {c["dataset_id"] for c in cases} == {expected_id}

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


class TestDefinitionTreeLoadedOnce:
    """Spec §22.12：同一请求里同一份定义事实只装载一次。

    这条断言比"耗时可接受"更耐久——它钉住的是原因（重复装载），不是结果（毫秒数）。
    """

    @staticmethod
    def _count_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from agent_eval.loading import loader as loader_mod

        calls: list[str] = []
        original = loader_mod.load_dataset

        def counting(root: Path, ref: str):
            calls.append(ref)
            return original(root, ref)

        monkeypatch.setattr(loader_mod, "load_dataset", counting)
        return calls

    def test_suites_endpoint_loads_each_dataset_once(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._count_loads(monkeypatch)
        assert client.get("/api/suites").status_code == 200
        assert calls, "该端点应当装载定义树"
        assert sorted(calls) == sorted(set(calls)), f"同一 dataset 被重复装载：{calls}"

    def test_security_endpoint_loads_each_dataset_once(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """套件计数与红队覆盖矩阵读同一份 case：不能各装一遍。"""
        calls = self._count_loads(monkeypatch)
        assert client.get("/api/security").status_code == 200
        assert calls, "该端点应当装载定义树"
        assert sorted(calls) == sorted(set(calls)), f"同一 dataset 被重复装载：{calls}"


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
        assert diffs, "两侧 run 至少应有一个可比的 run-level metric"
        # 同一个 mock 跑两次：所有 run-level metric 都必须判 unchanged。
        # tokens / tool_calls / task_success 是确定性的；latency_ms 是 wall-clock，
        # 但 run-level diff 现在带噪声下限（毫秒以下判 unchanged，阈值与 case 级
        # 性能回归同源，ROADMAP 发现的 1）——此前这里的 latency 例外已摘除，
        # 摘除本身就是对该修复的回归断言。
        assert set(diffs.values()) == {"unchanged"}, diffs
