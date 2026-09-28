"""语义 diff 的归一化规则表（PRD §57 semantic / SQL AST diff，Spec §20）。

PRD §57 要求 Tool Arguments 支持两级 diff。这个文件只测一件事：
**归一化规则是否恰好覆盖它该覆盖的，且不多覆盖**。

每条规则都成对出现：判相同的用例 + **不该被归一化掉的对照用例**。
只有前半会得到一个"把什么都判成相同"的实现——那是回归平台里最危险的方向：
误报让人多点一次确认，漏判让真实回归静默通过（PRD §57 的风险段落）。
"""

from __future__ import annotations

import io

import pytest

from agent_eval.models.regression import GateReport, RegressionComparison, TraceDiff
from agent_eval.models.results import CaseRunResult, CaseStatus, ToolCallRecord
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.regression.compare import compare_runs
from agent_eval.regression.semantic import (
    ComparisonKind,
    compare_values,
    normalize_path,
    numeric_value,
)
from agent_eval.regression.sql_diff import (
    ast_available,
    dialect_for,
    looks_like_sql,
    normalize_sql,
)
from agent_eval.regression.trace_diff import diff_case_runs
from agent_eval.reports.aggregate import RunAggregate
from agent_eval.reports.html import render_html


class TestSqlTextNormalization:
    """文本层归一化（无依赖）：关键字大小写、空白、尾随分号。

    这一层的存在意义是：**没装 sqlglot 时也不能把"只是大小写不同"报成差异**。
    所以它的每一条规则都必须能独立成立，且必须守住"字面量不是关键字"这条线。
    """

    @pytest.mark.parametrize(
        "a,b",
        [
            ("SELECT  *  FROM  t", "select * from t"),
            ("SELECT 1;", "select 1"),
            ("SELECT 1 ; ", "SELECT 1"),
            ("SELECT * FROM t\nWHERE id = 1", "select * from t where id = 1"),
        ],
    )
    def test_text_level_equivalences(self, a: str, b: str) -> None:
        assert normalize_sql(a) == normalize_sql(b)

    @pytest.mark.parametrize(
        "a,b",
        [
            # 归一化不得吃掉这些：它们语义上真的不同
            ("SELECT * FROM t WHERE id = 1", "SELECT * FROM t WHERE id = 2"),
            ("SELECT COUNT(*) FROM t", "SELECT COUNT(id) FROM t"),
            ("SELECT * FROM t", "SELECT * FROM t2"),
            # 字符串字面量的大小写是数据，不跟着关键字一起折叠
            ("SELECT * FROM t WHERE name = 'ACME'", "SELECT * FROM t WHERE name = 'acme'"),
            # 字面量内部的空白同理
            ("SELECT * FROM t WHERE name = 'a  b'", "SELECT * FROM t WHERE name = 'a b'"),
        ],
    )
    def test_text_level_non_equivalences(self, a: str, b: str) -> None:
        assert normalize_sql(a) != normalize_sql(b)

    def test_string_literal_is_preserved_verbatim(self) -> None:
        assert normalize_sql("SELECT 'A B'") == "select 'A B'"

    def test_escaped_quote_does_not_end_the_literal(self) -> None:
        """`''` 是转义引号：漏判会让后面的字符被当关键字折叠掉。"""
        assert normalize_sql("SELECT 'it''s A B'") == "select 'it''s A B'"

    def test_operator_spacing_is_left_to_the_ast_layer(self) -> None:
        """`id = 1` 与 `id=1` **不在**文本层归一化。

        在文本层删标点周围的空白会制造语法陷阱：``a - -b`` 去掉空格就成了注释
        ``--``。这条等价关系交由 AST 判，代价（依赖 sqlglot）由降级标注说明。
        """
        assert normalize_sql("SELECT * FROM t WHERE id = 1") != normalize_sql(
            "SELECT * FROM t WHERE id=1"
        )
        # 但比对结论仍然是"相同"——因为 AST 层接住了
        assert compare_values(
            "SELECT * FROM t WHERE id = 1", "select * from t where id=1", sql_dialect="sqlite"
        ).same


class TestSqlSemanticComparison:
    """语义比对：文本层不够时上 AST，且结论带层级。"""

    def test_keyword_case_is_semantic_not_structural(self) -> None:
        verdict = compare_values("SELECT  *  FROM  t", "select * from t", sql_dialect="sqlite")
        assert verdict.same and verdict.kind == ComparisonKind.semantic

    def test_operator_spacing_needs_the_ast_layer(self) -> None:
        verdict = compare_values(
            "SELECT * FROM t WHERE id = 1", "select * from t where id=1", sql_dialect="sqlite"
        )
        assert verdict.same, verdict
        assert verdict.kind == ComparisonKind.ast

    def test_ast_level_catches_what_text_cannot(self) -> None:
        """`count( * )` 与 `COUNT(*)` 归一化后文本仍不同，靠 AST 判相同。"""
        verdict = compare_values(
            "SELECT count( * ) FROM orders", "SELECT COUNT(*) FROM orders", sql_dialect="sqlite"
        )
        assert verdict.same, verdict
        assert verdict.kind == ComparisonKind.ast

    @pytest.mark.parametrize(
        "a,b",
        [
            ('SELECT * FROM "orders"', "SELECT * FROM orders"),
            ("SELECT `orders`.`id` FROM `orders`", "SELECT orders.id FROM orders"),
            ("SELECT * FROM [orders]", "SELECT * FROM orders"),
        ],
    )
    def test_identifier_quotes_are_equivalent(self, a: str, b: str) -> None:
        verdict = compare_values(a, b, sql_dialect="sqlite")
        assert verdict.same, f"{a!r} vs {b!r}: {verdict}"

    @pytest.mark.parametrize(
        "a,b",
        [
            ("SELECT * FROM t WHERE id = 1", "SELECT * FROM t WHERE id = 2"),
            ("SELECT COUNT(*) FROM t", "SELECT COUNT(id) FROM t"),
            # ORDER BY 的方向不同 → 结果集不同
            ("SELECT * FROM t ORDER BY id ASC", "SELECT * FROM t ORDER BY id DESC"),
            # JOIN 条件不同
            ("SELECT * FROM a JOIN b ON a.id = b.id", "SELECT * FROM a JOIN b ON a.id = b.other"),
            # WHERE 与 HAVING 的作用面不同
            ("SELECT c FROM t WHERE c > 1", "SELECT c FROM t HAVING c > 1"),
        ],
    )
    def test_ast_level_non_equivalences(self, a: str, b: str) -> None:
        """归一化不能过度：这些必须仍判不同。"""
        assert not compare_values(a, b, sql_dialect="sqlite").same, f"{a!r} vs {b!r}"

    def test_dialect_comes_from_declaration(self) -> None:
        assert dialect_for("sqlite") == "sqlite"
        assert dialect_for("Postgres") == "postgres"
        assert dialect_for("postgresql") == "postgres"
        # 未声明的 provider 不猜：返回 None，由调用方决定默认方言并标注
        assert dialect_for("oracle") is None
        assert dialect_for(None) is None

    def test_invalid_sql_degrades_and_says_so(self) -> None:
        """parse 失败必须降级并标注——既不静默判"相同"也不静默判"不同"。

        这对 SQL 只在运算符空白上不同（文本层不折叠运算符空白，见上面那条），
        因此会真的走到 AST；又因为两边都不合法，parse 失败 → 降级按文本判"不同"。
        降级方向是"保留差异"，这是安全的一侧。
        """
        verdict = compare_values(
            "SELECT FROM WHERE a=1", "SELECT FROM WHERE a = 1", sql_dialect="sqlite"
        )
        assert verdict.degraded is not None
        assert "解析失败" in verdict.degraded
        assert verdict.same is False

    def test_text_level_equality_needs_no_parsing(self) -> None:
        """文本层判相同是**完整**结论：不必解析，也不应出现降级标注。

        归一化只折叠关键字大小写与空白（字面量原样保留），两条归一化后相同的
        字符串在语义上确实相同——再去解析一遍只是白花时间，还会给一个本来
        确定的结论挂上"降级"标签。
        """
        verdict = compare_values("SELECT ) FROM t", "select )  from  t", sql_dialect="sqlite")
        assert verdict.same is True
        assert verdict.degraded is None
        assert verdict.kind == ComparisonKind.semantic

    def test_invalid_sql_that_differs_stays_different(self) -> None:
        verdict = compare_values(
            "SELECT FROM WHERE a=1", "SELECT FROM WHERE b = 2", sql_dialect="sqlite"
        )
        assert verdict.same is False
        assert verdict.degraded is not None

    def test_unparseable_does_not_become_equal(self) -> None:
        """降级绝不能把"判断不了"变成"相同"。"""
        verdict = compare_values(
            "SELECT * FROM t WHERE id = ??", "SELECT * FROM t WHERE id = 1", sql_dialect="sqlite"
        )
        assert verdict.same is False

    def test_sql_detection_is_head_anchored(self) -> None:
        assert looks_like_sql("SELECT * FROM t")
        assert looks_like_sql("  with x as (select 1) select * from x")
        # 参数里出现 "select" 的自然语言不该被当 SQL 送进解析器
        assert not looks_like_sql("请帮我 select 一个合适的方案")
        assert not looks_like_sql("orders.csv")
        assert not looks_like_sql("42")

    def test_ast_available_is_an_explicit_fact(self) -> None:
        assert ast_available() is True


class TestMissingSqlglotDegradation:
    """没装 sqlglot 时的降级路径（Spec §20.2 的 `sqlglot_missing`）。

    这条路径**必须**被测到：它是可选依赖的代价所在，而"本机恰好没装"不是一种
    测法——装了 sqlglot 的开发机与 CI 上它会静默消失，降级分支就再没人碰过。
    所以这里 monkeypatch 掉模块级的解析器入口，而不是靠环境。
    """

    @pytest.fixture()
    def no_sqlglot(self, monkeypatch: pytest.MonkeyPatch):
        from agent_eval.regression import sql_diff

        monkeypatch.setattr(sql_diff, "_parse_one", None)
        assert sql_diff.ast_available() is False
        return sql_diff

    def test_text_layer_still_works_without_it(self, no_sqlglot) -> None:
        """降级不等于失效：大小写与空白仍由文本层判相同（不附降级说明）。"""
        verdict = compare_values("SELECT 1", "select 1;", sql_dialect="sqlite")
        assert verdict.same is True
        assert verdict.kind == ComparisonKind.semantic
        assert verdict.degraded_kind is None

    def test_needs_ast_is_reported_as_missing_dep_not_as_different(self, no_sqlglot) -> None:
        """需要 AST 才能判等价的写法：判"不同"但如实标注缺少依赖。

        静默判"不同"会让用户以为参数真的变了；静默判"相同"更糟（漏判）。
        第三种才是正确处置：给出结论 + 说明这个结论是在哪一层得出的 + 补依赖的方法。
        """
        verdict = compare_values("SELECT count( * ) FROM t", "SELECT COUNT(*) FROM t")
        assert verdict.same is False
        assert verdict.degraded_kind == "sqlglot_missing"
        assert "sqlglot" in (verdict.degraded or "")

    def test_diff_notes_carry_the_missing_dep_summary(self, no_sqlglot) -> None:
        base = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT count( * ) FROM t"})
        )
        cand = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT COUNT(*) FROM t"}))
        diff = diff_case_runs(base, cand)
        assert [item.path for item in diff.argument_diffs] == ["sql"]
        assert len(diff.diff_notes) == 1
        # 摘要要给出"下一步动作"，不能只说"缺依赖"
        assert "uv sync" in diff.diff_notes[0]


class TestNumericNormalization:
    def test_int_and_float_are_equal(self) -> None:
        assert compare_values(1, 1.0).same
        assert compare_values(1.0, 1).same
        assert compare_values(1, 1.0).kind == ComparisonKind.semantic

    def test_numeric_strings_are_equal(self) -> None:
        assert compare_values("1", "1.0").same
        assert compare_values("5", "5.0").same
        assert compare_values("0.10", "0.1").same

    def test_different_numbers_stay_different(self) -> None:
        assert not compare_values(1, 2).same
        assert not compare_values("1", "2").same
        assert not compare_values(1.0, 1.0001).same

    def test_cross_type_numbers_stay_different(self) -> None:
        """Spec §20.1 的有意收窄：`1` 与 `"1"` 判不同。

        int/str 的边界是工具参数里真实存在的回归（下游把它们当不同的值），
        PRD §57 把它们列为一组，但判相同会漏掉这一类。
        """
        assert not compare_values(1, "1").same
        assert not compare_values(1.0, "1.0").same

    def test_bool_is_not_a_number(self) -> None:
        """`True == 1` 在 Python 里为真，在 JSON 里是两个值。"""
        assert not compare_values(True, 1).same
        assert not compare_values(False, 0).same
        assert compare_values(True, True).same

    def test_numeric_value_rejects_non_numbers(self) -> None:
        assert numeric_value("abc") is None
        assert numeric_value("1.2.3") is None
        assert numeric_value(True) is None
        assert numeric_value(None) is None
        assert numeric_value("1e3") == 1000


class TestPathNormalization:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("./a/b.csv", "a/b.csv"),
            ("a/b.csv", "a/b.csv/"),
            ("a//b.csv", "a/b.csv"),
            ("./a//b.csv/", "a/b.csv"),
        ],
    )
    def test_path_equivalences(self, a: str, b: str) -> None:
        assert normalize_path(a) == normalize_path(b)
        assert compare_values(a, b).same

    @pytest.mark.parametrize(
        "a,b",
        [
            ("a/b.csv", "a/c.csv"),
            ("a/b.csv", "ab.csv"),
            ("dir/sub/x", "dir/sub/y"),
        ],
    )
    def test_path_non_equivalences(self, a: str, b: str) -> None:
        assert not compare_values(a, b).same

    def test_parent_traversal_is_not_collapsed(self) -> None:
        """`a/../b` 与 `b` 的等价性依赖 cwd：不折叠，否则掩护了"它到底请求了哪个文件"。"""
        assert normalize_path("a/../b") == "a/../b"
        assert not compare_values("a/../b", "b").same


class TestNonStringValues:
    def test_dict_and_list_use_structural_equality(self) -> None:
        assert compare_values({"a": 1}, {"a": 1}).same
        assert not compare_values({"a": 1}, {"a": 2}).same
        assert compare_values([1, 2], [1, 2]).same
        assert not compare_values([1, 2], [2, 1]).same

    def test_none_vs_empty_string_stay_different(self) -> None:
        assert not compare_values(None, "").same
        assert compare_values(None, None).same


def _run(*calls: ToolCallRecord, database: str | None = "sqlite") -> CaseRunResult:
    return CaseRunResult(
        id="cr1",
        run_id="r1",
        case_id="c1",
        case_version=1,
        iteration=1,
        status=CaseStatus.PASS,
        tool_calls=list(calls),
        environment_database=database,
    )


class TestTraceDiffIntegration:
    """端到端：语义相同的参数不进 argument_diffs，但要出现在 semantic_equal 里。"""

    def test_semantically_equal_sql_is_not_reported_as_a_diff(self) -> None:
        base = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT * FROM t"}))
        cand = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "select * from t;"}))
        diff = diff_case_runs(base, cand)
        assert diff.argument_diffs == []
        assert [item.path for item in diff.semantic_equal] == ["sql"]
        assert diff.semantic_equal[0].comparison == "semantic"
        assert not diff.changed

    def test_really_different_sql_is_still_reported(self) -> None:
        base = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT * FROM t"}))
        cand = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "DELETE FROM t"}))
        diff = diff_case_runs(base, cand)
        assert [item.path for item in diff.argument_diffs] == ["sql"]
        # 走到了 AST 层：两侧都解析成功、AST 不同，所以结论的层级是 ast。
        # 层级标注的是"按哪一层判出来的"，不是"哪一层发现不同"——
        # structural 只表示"无需归一化就已判定"。
        assert diff.argument_diffs[0].comparison == "ast"
        assert diff.semantic_equal == []
        assert diff.changed

    def test_semantic_equal_never_lands_in_argument_diffs(self) -> None:
        """同一路径只能出现在一边：两处都报会让报告自相矛盾。"""
        base = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT 1", "limit": 5}))
        cand = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "select 1;", "limit": 5.0})
        )
        diff = diff_case_runs(base, cand)
        reported = {item.path for item in diff.argument_diffs}
        equal = {item.path for item in diff.semantic_equal}
        assert reported.isdisjoint(equal)
        assert equal == {"sql", "limit"}  # limit: 5 与 5.0 是语义相同

    def test_dialect_comes_from_case_declaration(self) -> None:
        """方言取自 CaseRunResult 的反范式化副本（Spec §20.3）。"""
        base = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT count( * ) FROM t"}),
            database="sqlite",
        )
        cand = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT COUNT(*) FROM t"}),
            database="sqlite",
        )
        diff = diff_case_runs(base, cand)
        assert diff.argument_diffs == []
        assert diff.semantic_equal[0].comparison == "ast"

    def test_missing_dialect_is_noted_not_guessed_silently(self) -> None:
        base = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT count( * ) FROM t"}),
            database=None,
        )
        cand = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT COUNT(*) FROM t"}),
            database=None,
        )
        diff = diff_case_runs(base, cand)
        assert diff.argument_diffs == []
        assert any("未声明 environment.database" in note for note in diff.diff_notes)

    def test_degradation_is_surfaced_in_diff_notes(self) -> None:
        """非法 SQL：判"不同"的置信度更低（无法上 AST），这件事必须出现在报告里。"""
        base = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT FROM WHERE (("}))
        cand = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT FROM WHERE (("}))
        diff = diff_case_runs(base, cand)
        # 两侧逐字相同 → 走 structural 快路径，连解析都不必做（也不该做），也就不该有降级
        assert diff.argument_diffs == [] and diff.semantic_equal == []
        assert diff.diff_notes == []

        base2 = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT FROM WHERE a=1"}))
        cand2 = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT FROM WHERE a = 1"})
        )
        diff2 = diff_case_runs(base2, cand2)
        assert [item.path for item in diff2.argument_diffs] == ["sql"]
        assert any("解析失败" in note for note in diff2.diff_notes)

    def test_parse_failure_hint_is_deduped(self) -> None:
        """同一原因只报一次：几十个参数各自带一行同样的降级说明是噪声。"""
        base = _run(
            *[
                ToolCallRecord(name="execute_sql", arguments={"sql": f"SELECT FROM WHERE a={i}"})
                for i in range(4)
            ]
        )
        cand = _run(
            *[
                ToolCallRecord(name="execute_sql", arguments={"sql": f"SELECT FROM WHERE a = {i}"})
                for i in range(4)
            ]
        )
        diff = diff_case_runs(base, cand)
        assert len(diff.diff_notes) == 1, diff.diff_notes


class TestCompareRunsWiring:
    """接线：`compare_runs` 只在 REGRESSION/IMPROVED/FLAKY 的 case 上算 trace diff（PRD §56），
    但方言（Spec §20.3）与语义相同项必须穿过这一层到达报告，不能只活在 `diff_case_runs` 里。

    单元测试全绿不代表接线通——观测面或参数断了只会静默变成"没有语义相同项"。
    """

    @staticmethod
    def _meta(run_id: str) -> RunMetadata:
        return RunMetadata(
            run_id=run_id,
            benchmark_id="bench",
            dataset_id="d",
            dataset_version="v1",
            dataset_hash="h",
            profile="mock",
            status=RunStatus.completed,
        )

    @staticmethod
    def _side(run_id: str, call: ToolCallRecord, status: CaseStatus, iterations: int = 3):
        """同一 case 的 N 次 iteration（默认 3：Spec §3.3 的判定表只在 ≥3 次有效
        观测上才给 STABLE_PASS / STABLE_FAIL，少于 3 次一律 UNKNOWN → UNDETERMINED）。"""
        return [
            _run(call).model_copy(
                update={"run_id": run_id, "status": status, "iteration": index + 1}
            )
            for index in range(iterations)
        ]

    def test_semantic_equal_reaches_the_comparison(self) -> None:
        base = self._side(
            "run_base",
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT count( * ) FROM t"}),
            CaseStatus.PASS,
        )
        cand = self._side(
            "run_cand",
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT COUNT(*) FROM t"}),
            CaseStatus.FAIL,
        )
        comparison = compare_runs(
            self._meta("run_base"),
            base,
            self._meta("run_cand"),
            cand,
            # trace diff 只在提供了 span 加载器时计算（PRD §56）；空 span 列表足够——
            # 本用例要验的是参数语义比对这条链路，不是 span 计数。
            trace_loader=lambda _case_run: {"baseline": [], "candidate": []},
        )
        # 基线稳定 PASS、候选稳定 FAIL → REGRESSION，于是 trace diff 被计算（PRD §56）
        assert [c.state.value for c in comparison.cases] == ["REGRESSION"]
        assert len(comparison.trace_diffs) == 1
        diff = comparison.trace_diffs[0]
        assert diff.argument_diffs == []
        assert [item.path for item in diff.semantic_equal] == ["sql"]
        # 方言取自两侧结果的反范式化副本：sqlite 声明 → 走完 AST 层（Spec §20.3）
        assert diff.semantic_equal[0].comparison == "ast"

    def test_unchanged_cases_get_no_trace_diff(self) -> None:
        """口径不变：两侧都稳定 PASS 的 case 不算 trace diff（PRD §56 只标注变化的）。

        本次改动放宽的是**呈现筛选**（报告/CLI 展示 semantic_equal），不是
        "哪些 case 参与比对"——把后者也放宽会让报告在每次 run 里都刷出全量 trace。
        """
        call = ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT 1"})
        base = self._side("run_base", call, CaseStatus.PASS)
        cand = self._side("run_cand", call, CaseStatus.PASS)
        comparison = compare_runs(self._meta("run_base"), base, self._meta("run_cand"), cand)
        assert [c.state.value for c in comparison.cases] == ["UNCHANGED"]
        assert comparison.trace_diffs == []


def _aggregate_with(diff: TraceDiff) -> RunAggregate:
    """最小可渲染的 RunAggregate：只挂一份 trace diff（报告侧的唯一输入）。"""
    counts = {
        "cases": 0,
        "iterations": 0,
        "passed_iterations": 0,
        "failed_iterations": 0,
        "error_iterations": 0,
        "flaky_cases": 0,
        "regression_cases": 0,
        "improved_cases": 0,
    }
    return RunAggregate(
        run=RunMetadata(
            run_id="run_cand",
            benchmark_id="bench",
            dataset_id="d",
            dataset_version="v1",
            dataset_hash="h",
            profile="mock",
            status=RunStatus.completed,
        ),
        cases=[],
        counts=counts,
        metrics={},
        verdict="pass",
        comparison=RegressionComparison(
            baseline_run_id="run_base",
            candidate_run_id="run_cand",
            benchmark_id="bench",
            trace_diffs=[diff],
        ),
    )


class TestDiffVisibility:
    """PRD §57 的呈现要求：用户必须能看到"这是语义级相同"（Spec §20.2）。

    没有这一条，"参数差异为空"在报告里与"这次根本没比"长得一模一样。
    """

    def test_report_html_shows_semantic_equal_and_notes(self) -> None:
        base = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT count( * ) FROM t"}),
            database=None,
        )
        cand = _run(
            ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT COUNT(*) FROM t"}),
            database=None,
        )
        diff = diff_case_runs(base, cand)
        assert diff.semantic_equal and diff.diff_notes and not diff.changed

        html = render_html(
            _aggregate_with(diff), GateReport(run_id="run_cand", gate="release", verdict="pass")
        )
        # 一个 changed=False 的 case 也必须进报告：它的信息量在 semantic_equal / diff_notes 上
        assert "Trace Diff" in html
        assert "语义相同" in html
        assert "SELECT COUNT(*)" in html
        assert "未声明 environment.database" in html

    def test_cli_prints_semantic_equal_and_notes(self) -> None:
        """`cli compare` 的空 diff 也要说清"哪些路径是语义相同"。"""
        from rich.console import Console

        from agent_eval.cli.commands import compare_cmd

        base = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "SELECT 1"}))
        cand = _run(ToolCallRecord(name="execute_sql", arguments={"sql": "select 1;"}))
        comparison = RegressionComparison(
            baseline_run_id="run_base",
            candidate_run_id="run_cand",
            benchmark_id="bench",
            trace_diffs=[diff_case_runs(base, cand)],
        )
        recorder = Console(file=io.StringIO(), width=200, no_color=True)
        original = compare_cmd.console
        compare_cmd.console = recorder
        try:
            compare_cmd._print_trace_diffs(comparison)
        finally:
            compare_cmd.console = original
        output = recorder.file.getvalue()
        assert "语义相同" in output
        assert "execute_sql.sql" in output
