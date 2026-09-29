"""门禁失真类缺陷的回归网（本轮审计修复）。

每条用例对应一个"配置看着生效、实际空转"或"判定方向反了"的缺陷：

1. ``forbidden_paths`` 曾是子串匹配 → `/var/etc/passwd` 命中 `/etc/passwd`（假阳性）。
2. ``tool_arguments`` 曾早退（任一调用匹配即 pass）→ 两次调用错一次仍判过（漏判）。
3. junit 的 ``skipped`` 分支曾不可达 → 全 skipped 的 case 以 passed 形态进 CI 报告。
4. ``hard_failure_categories`` 曾被 YAML 解析、被 REST 回显，但求值器从不读取。
5. ``resolve_metric`` 曾把 ``custom.*`` 与拼错的 ``native.*`` 静默丢弃。
"""

from __future__ import annotations

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
