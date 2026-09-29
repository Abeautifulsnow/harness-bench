"""门禁失真类缺陷的回归网（本轮审计修复）。

每条用例对应一个"配置看着生效、实际空转"或"判定方向反了"的缺陷：

1. ``forbidden_paths`` 曾是子串匹配 → `/var/etc/passwd` 命中 `/etc/passwd`（假阳性）。
2. ``tool_arguments`` 曾早退（任一调用匹配即 pass）→ 两次调用错一次仍判过（漏判）。
3. junit 的 ``skipped`` 分支曾不可达 → 全 skipped 的 case 以 passed 形态进 CI 报告。
4. ``hard_failure_categories`` 曾被 YAML 解析、被 REST 回显，但求值器从不读取。
5. ``resolve_metric`` 曾把 ``custom.*`` 与拼错的 ``native.*`` 静默丢弃。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_eval.errors import MetricUnavailableError
from agent_eval.evaluators.native import EvalScope, _check_tool_arguments, _group_score
from agent_eval.evaluators.registry import resolve_metric
from agent_eval.models.case import Assertion, SecurityAssertion, ToolArgumentMatcher
from agent_eval.models.profile import MetricSpec
from agent_eval.models.results import CaseRunResult, CaseStatus, MetricResultModel, ToolCallRecord
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.quality.gates import GateRules, evaluate_gate
from agent_eval.reports.aggregate import build_aggregate, case_status_for_junit
from agent_eval.security.evaluator import evaluate_security


def _scope(tool_calls: list[ToolCallRecord]) -> EvalScope:
    return EvalScope(run_status="success", final_output="out", tool_calls=tool_calls)


class TestForbiddenPathPrefix:
    """Spec §12.1：路径规则是**前缀匹配**，不是子串包含。"""

    def _findings(self, path: str, forbidden: list[str]) -> dict[str, str]:
        calls = [ToolCallRecord(name="read_file", arguments={"path": path})]
        return {
            f.rule: f.verdict
            for f in evaluate_security(
                SecurityAssertion(forbidden_paths=forbidden, forbidden_tools=["never"]),
                calls,
            )
        }

    def test_variant_path_is_not_a_hit(self) -> None:
        """`/var/etc/passwd` 与 `/etc/passwd` 是两条不同路径，不得判违规。"""
        verdicts = self._findings("/var/etc/passwd", ["/etc/passwd"])
        assert verdicts["security.forbidden_path"] == "pass"

    def test_suffix_variant_is_not_a_hit(self) -> None:
        """`/etc/passwd_backup` 不在 `/etc/passwd` 之下。"""
        assert (
            self._findings("/etc/passwd_backup", ["/etc/passwd"])["security.forbidden_path"]
            == "pass"
        )

    def test_exact_path_is_a_hit(self) -> None:
        assert self._findings("/etc/passwd", ["/etc/passwd"])["security.forbidden_path"] == "fail"

    def test_descendant_path_is_a_hit(self) -> None:
        """真正的下级：`/etc/passwd/extra` 在被禁路径**之下**，判违规。"""
        verdicts = self._findings("/etc/passwd/extra", ["/etc/passwd"])
        assert verdicts["security.forbidden_path"] == "fail"

    def test_sibling_with_same_stem_is_not_a_hit(self) -> None:
        """`/etc/passwd.d` 是同级另一条路径（组件名更长），不是下级。"""
        assert (
            self._findings("/etc/passwd.d/extra", ["/etc/passwd"])["security.forbidden_path"]
            == "pass"
        )

    def test_path_inside_command_string_is_a_hit(self) -> None:
        assert (
            self._findings("cat /etc/passwd | head", ["/etc/passwd"])["security.forbidden_path"]
            == "fail"
        )

    def test_dotfile_does_not_match_longer_name(self) -> None:
        """`.env` 不得命中 `.envrc`；但 `/app/.env` 要命中。"""
        assert self._findings("/app/.envrc", [".env"])["security.forbidden_path"] == "pass"
        assert self._findings("/app/.env", [".env"])["security.forbidden_path"] == "fail"

    def test_trailing_slash_directory_declaration_is_a_hit(self) -> None:
        """目录式声明（尾分隔符）与不带斜杠同义：`/opt/secret/` 要罩住其下所有文件。

        回归：边界判定曾把声明值末尾的 `/` 后的正常文件名当成"更长字面量"，
        于是 `~/.ssh/`、`/etc/` 这类写法让规则静默失效（旧子串实现是命中的）。
        """
        assert (
            self._findings("/opt/secret/key.txt", ["/opt/secret/"])["security.forbidden_path"]
            == "fail"
        )
        assert self._findings("cat ~/.ssh/id_rsa", ["~/.ssh/"])["security.forbidden_path"] == "fail"

    def test_trailing_slash_declaration_still_rejects_sibling(self) -> None:
        """规范化只剥尾分隔符，不放松组件边界：`/etc/` 不得命中 `/etcX/foo`。"""
        assert self._findings("/etcX/foo", ["/etc/"])["security.forbidden_path"] == "pass"

    def test_backslash_directory_declaration_is_a_hit(self) -> None:
        """Windows 目录式声明同样要生效：`secrets\\` 罩住 `D:\\secrets\\k.txt`。"""
        assert (
            self._findings("D:\\secrets\\k.txt", ["secrets\\"])["security.forbidden_path"] == "fail"
        )

    def test_empty_declaration_matches_nothing(self) -> None:
        """剥离后为空的声明没有命名任何路径，不得放大成"匹配一切绝对路径"。"""
        assert self._findings("/opt/anything", [""])["security.forbidden_path"] == "pass"


class TestMatcherReasonRedaction:
    """reason 里回显的实际值必须脱敏（Spec §12.3 口径），且与匹配器种类无关。

    回归：`_mask` 只接进了 `exact` 分支，`contains` / `regex` 失败时把原文写进
    reason —— 而 reason 会进 PR 评论与工单，`tool_arguments` 恰恰常在断言里
    比对凭据类参数。
    """

    def test_contains_masks_secret_shaped_value(self) -> None:
        reason = ToolArgumentMatcher(contains="zzz").check("token=abcdefghijklmnop")
        assert reason is not None
        assert "abcdefghijklmnop" not in reason
        assert "token=abcd***" in reason

    def test_contains_masks_serialized_dict(self) -> None:
        reason = ToolArgumentMatcher(contains="zzz").check({"password": "hunter2xyzsecret"})
        assert reason is not None
        assert "hunter2xyzsecret" not in reason

    def test_regex_masks_secret_shaped_value(self) -> None:
        reason = ToolArgumentMatcher(regex="^SELECT").check("token=abcdefghijklmnop")
        assert reason is not None
        assert "abcdefghijklmnop" not in reason

    def test_exact_branch_still_masks(self) -> None:
        reason = ToolArgumentMatcher(exact="zzz").check("token=abcdefghijklmnop")
        assert reason is not None
        assert "abcdefghijklmnop" not in reason


class TestToolArgumentsAllOccurrence:
    """Spec §11.2：**每个 occurrence 都要通过**，全部通过才 pass。"""

    def _assertion(self, **matchers) -> Assertion:
        return Assertion.model_validate(
            {"extensions": {"tool_arguments": {"execute_sql": matchers}}}
        )

    def test_one_bad_call_among_two_fails(self) -> None:
        assertion = self._assertion(**{"filters.status": {"exact": "paid"}})
        scope = _scope(
            [
                ToolCallRecord(name="execute_sql", arguments={"filters": {"status": "paid"}}),
                ToolCallRecord(name="execute_sql", arguments={"filters": {"status": "pending"}}),
            ]
        )
        problems = _check_tool_arguments(assertion, scope)
        assert problems, "同一工具两次调用错一次必须判 fail（曾经早退成 pass）"
        assert "call #2" in problems[0]

    def test_all_calls_pass(self) -> None:
        assertion = self._assertion(**{"filters.status": {"exact": "paid"}})
        scope = _scope(
            [
                ToolCallRecord(name="execute_sql", arguments={"filters": {"status": "paid"}}),
                ToolCallRecord(name="execute_sql", arguments={"filters": {"status": "paid"}}),
            ]
        )
        assert _check_tool_arguments(assertion, scope) == []

    def test_score_is_continuous(self) -> None:
        """Spec §11.2：score = 通过的检查数 / 检查总数。"""
        assertion = self._assertion(**{"filters.status": {"exact": "paid"}})
        scope = _scope(
            [
                ToolCallRecord(name="execute_sql", arguments={"filters": {"status": "paid"}}),
                ToolCallRecord(name="execute_sql", arguments={"filters": {"status": "pending"}}),
            ]
        )
        assert _group_score("native.argument_checks", assertion, scope, False) == 0.5

    def test_contains_serializes_non_strings(self) -> None:
        """非字符串值先做 JSON 序列化再匹配（`{"id": 1}` 的 contains "id"）。"""
        assertion = Assertion.model_validate(
            {"extensions": {"tool_arguments": {"t": {"payload": {"contains": "id"}}}}}
        )
        scope = _scope([ToolCallRecord(name="t", arguments={"payload": {"id": 1}})])
        assert _check_tool_arguments(assertion, scope) == []


class TestJunitSkippedReachable:
    """Spec §6.3 + §19.1.1：全 skipped 的 case 必须是 skipped，不是 passed。"""

    def _meta(self) -> RunMetadata:
        return RunMetadata(
            run_id="run_x",
            benchmark_id="b1",
            dataset_id="d1",
            dataset_version="v1",
            dataset_hash="h1",
            profile="p",
            status=RunStatus.completed,
            suites_covered={},
        )

    def test_case_with_no_evaluated_metric_is_skipped(self) -> None:
        result = CaseRunResult(
            id="cr1",
            run_id="run_x",
            case_id="c1",
            case_version=1,
            iteration=1,
            status=CaseStatus.PASS,
            metric_results=[
                MetricResultModel(
                    id="mr1",
                    case_run_id="cr1",
                    metric="native.status",
                    evaluator="native",
                    verdict="skipped",
                    blocking=False,
                    reason="observation unavailable",
                )
            ],
        )
        aggregate = build_aggregate(self._meta(), [result])
        case = aggregate.cases[0]
        assert case.evaluated_metrics == 0
        assert case_status_for_junit(case) == "skipped"

    def test_case_with_evaluated_metric_is_passed(self) -> None:
        result = CaseRunResult(
            id="cr1",
            run_id="run_x",
            case_id="c1",
            case_version=1,
            iteration=1,
            status=CaseStatus.PASS,
            metric_results=[
                MetricResultModel(
                    id="mr1",
                    case_run_id="cr1",
                    metric="native.status",
                    evaluator="native",
                    verdict="pass",
                    blocking=False,
                    reason="ok",
                )
            ],
        )
        aggregate = build_aggregate(self._meta(), [result])
        assert case_status_for_junit(aggregate.cases[0]) == "passed"


class TestJunitSkippedEndToEnd:
    """junit 的 ``skipped`` 必须是**真实 run 能走到**的分支，不只是单测里拼的对象。

    此前这条分支只有上面那组手工构造的用例覆盖（Spec §22.12 记的"未修建议项"），
    而"单测能构造"与"真实链路能产出"是两件事：判 skipped 的 metric 需要在
    Runner 里被真的产出、聚合、写进 junit.xml。这里跑真实 Runner，断言落盘的
    文件内容。

    构造方式与仓库既有范式一致（`test_harness_evaluators`、`test_case_coverage`）：
    在临时 evals 树里新写 dataset / benchmark / suite / profile，**不动示例数据集
    的套件组成**。
    """

    ROOT = Path(__file__).resolve().parents[1]

    def _tree(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """临时 evals 树 + fixtures + data_root；返回 (evals_root, fixtures, data_root)。"""
        import shutil

        evals_root = tmp_path / "evals"
        shutil.copytree(self.ROOT / "evals", evals_root)
        fixtures = tmp_path / "fixtures"
        shutil.copytree(self.ROOT / "fixtures", fixtures)
        data_root = tmp_path / "data"
        data_root.mkdir()
        return evals_root, fixtures, data_root

    def _write_benchmark(self, evals_root: Path, *, profile: str) -> None:
        import yaml

        (evals_root / "benchmarks" / "junit-skip.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "junit-skip",
                    "dataset": "junitskip@1.0.0",
                    "suites": ["js"],
                    "default_profile": profile,
                }
            ),
            encoding="utf-8",
        )
        (evals_root / "suites" / "js.yaml").write_text(
            yaml.safe_dump({"name": "js", "tags": ["js"]}), encoding="utf-8"
        )

    def _write_dataset(self, evals_root: Path, *, expect: dict | None) -> None:
        import yaml

        ds = evals_root / "datasets" / "junitskip"
        (ds / "cases").mkdir(parents=True)
        (ds / "dataset.yaml").write_text("id: junitskip\nversion: 1.0.0\n", encoding="utf-8")
        body: dict = {
            "id": "js.unjudged",
            "version": 1,
            "name": "js.unjudged",
            "tags": ["js"],
            "input": {"type": "single_turn", "prompt": "ping"},
            "execution": {"timeout": 15, "repeat": 1},
        }
        if expect is not None:
            body["expected"] = expect
        (ds / "cases" / "js.unjudged.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")

    async def _run(self, tmp_path: Path, *, profile_body: dict, expect: dict | None) -> Path:
        """跑真实 Runner，返回 run 目录。"""
        import yaml

        from agent_eval.runner.runner import RunConfig, Runner

        evals_root, fixtures, data_root = self._tree(tmp_path)
        self._write_dataset(evals_root, expect=expect)
        self._write_benchmark(evals_root, profile="unjudged")
        (evals_root / "profiles" / "unjudged.yaml").write_text(
            yaml.safe_dump({"name": "unjudged", **profile_body}), encoding="utf-8"
        )
        cfg = RunConfig(
            evals_root=evals_root,
            fixtures_root=fixtures,
            data_root=data_root,
            benchmark="junit-skip",
            agent_endpoint="fake://",
            gate="pr",
            no_judge=True,
        )
        outcome = await Runner(cfg).run()
        return data_root / "runs" / outcome.run_id

    async def test_all_skipped_case_writes_skipped_to_junit(self, tmp_path: Path) -> None:
        """路径一：case 不声明任何期望 + profile 只挂"参数未声明即 skipped"的插件。"""
        import xml.etree.ElementTree as ET

        run_dir = await self._run(
            tmp_path,
            profile_body={
                "metrics": [
                    {"id": "harness.skill_load", "blocking": False},
                    {"id": "harness.mcp_permission", "blocking": False},
                ]
            },
            expect=None,
        )
        xml = ET.parse(run_dir / "junit.xml").getroot()
        assert xml.get("skipped") == "1", ET.tostring(xml, encoding="unicode")
        testcase = xml.find("testcase")
        assert testcase is not None
        skipped = testcase.find("skipped")
        assert skipped is not None, "全 skipped 的 case 必须渲染成 <skipped>，不能是 passed"

    async def test_observation_unavailable_writes_skipped_to_junit(self, tmp_path: Path) -> None:
        """路径二：只声明 ``max_cost`` 且无定价表 → ``ObservationUnavailable`` 判 skipped。

        PRD §59 / Spec §19 的 `null ≠ 0`：拿不到成本时不能当 0 过闸。
        """
        import xml.etree.ElementTree as ET

        run_dir = await self._run(
            tmp_path,
            profile_body={"metrics": [{"id": "harness.skill_load", "blocking": False}]},
            expect={"constraints": {"max_cost": 0.01}},
        )
        xml = ET.parse(run_dir / "junit.xml").getroot()
        assert xml.get("skipped") == "1", ET.tostring(xml, encoding="unicode")
        assert xml.find("testcase/skipped") is not None

    async def test_run_level_warning_names_unjudged_cases(self, tmp_path: Path) -> None:
        """可见性：零验证的 run 必须在 warnings 里说清哪些 case 没被真判过。

        当前口径下这类 run **仍然判 pass**（判定口径变更由独立增量决定，见 Spec
        §22.12 的收口说明），所以 warnings 是 CI 上唯一的可见信号——它必须存在且
        指名道姓，只报个数等于让读者自己去翻 junit。
        """
        import json
        import xml.etree.ElementTree as ET

        run_dir = await self._run(
            tmp_path,
            profile_body={
                "metrics": [
                    {"id": "harness.skill_load", "blocking": False},
                    {"id": "harness.mcp_permission", "blocking": False},
                ]
            },
            expect=None,
        )
        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        unjudged = [w for w in report["warnings"] if w.startswith("UNJUDGED")]
        assert len(unjudged) == 1, report["warnings"]
        assert "js.unjudged" in unjudged[0]
        # 口径未变：零验证的 run 仍判 pass，warning 只负责可见
        assert report["verdict"] == "pass"

        # warning 与 junit 的 skipped 计数同源
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        junit_skipped = int(ET.parse(run_dir / "junit.xml").getroot().get("skipped", "0"))
        assert gate["aggregate"]["skipped"] == junit_skipped == 1


class TestHardFailureCategoriesConsumed:
    """``hard_failure_categories`` 必须真的参与求值，而不是只被解析与回显。"""

    def _aggregate(self, tags: list[str], reason: str = "expected to contain 'x', got ''"):
        result = CaseRunResult(
            id="cr1",
            run_id="run_x",
            case_id="c1",
            case_version=1,
            iteration=1,
            status=CaseStatus.FAIL,
            case_tags=tags,
            metric_results=[
                MetricResultModel(
                    id="mr1",
                    case_run_id="cr1",
                    metric="native.output_checks",
                    evaluator="native",
                    verdict="fail",
                    blocking=True,
                    reason=reason,
                )
            ],
        )
        meta = RunMetadata(
            run_id="run_x",
            benchmark_id="b1",
            dataset_id="d1",
            dataset_version="v1",
            dataset_hash="h1",
            profile="p",
            status=RunStatus.completed,
        )
        return build_aggregate(meta, [result])

    def _gate_rule(self, aggregate, declared: list[str]):
        rules = GateRules(gate="pr", hard_failure_categories=declared)
        report = evaluate_gate(aggregate, rules)
        return next((r for r in report.rules if r.rule == "hard_failure_categories"), None)

    def test_declared_metric_id_blocks(self) -> None:
        aggregate = self._aggregate([])
        rule = self._gate_rule(aggregate, ["native.output_checks"])
        assert rule is not None and rule.verdict == "fail"
        assert rule.affected_case_runs == ["c1"]

    def test_declared_subcategory_blocks(self) -> None:
        """声明二级分类（agent.incomplete）时，按 taxonomy 归类的失败也要命中。"""
        aggregate = self._aggregate([])
        rule = self._gate_rule(aggregate, ["agent.incomplete"])
        assert rule is not None and rule.verdict == "fail"

    def test_undeclared_category_does_not_block_by_this_rule(self) -> None:
        aggregate = self._aggregate([])
        assert self._gate_rule(aggregate, ["security.forbidden_tool"]) is None or (
            self._gate_rule(aggregate, ["security.forbidden_tool"]).verdict == "pass"
        )

    def test_no_declaration_produces_no_rule(self) -> None:
        aggregate = self._aggregate([])
        assert self._gate_rule(aggregate, []) is None


class TestMetricResolutionFailsFast:
    """Spec §7.4：不可求值的 metric 必须 fail-fast，不得静默丢弃。"""

    def test_custom_namespace_is_rejected(self) -> None:
        with pytest.raises(MetricUnavailableError):
            resolve_metric(MetricSpec(id="custom.answer_quality"), {}, no_judge=False)

    def test_typo_native_metric_is_rejected(self) -> None:
        with pytest.raises(MetricUnavailableError):
            resolve_metric(MetricSpec(id="native.output_check"), {}, no_judge=False)
