"""Native Evaluator 测试：断言词汇表全量 + 扩展字段。"""

from __future__ import annotations

from agent_eval.evaluators.native import (
    EvalScope,
    evaluate_assertions,
    synthesize_platform_verdicts,
    unsupported_declarations,
)
from agent_eval.models.case import Assertion
from agent_eval.models.results import ToolCallRecord


def scope(**kw) -> EvalScope:
    defaults = dict(
        run_status="success",
        final_output="QUERY COMPLETE: 5 rows",
        tool_calls=[ToolCallRecord(name="database_schema"), ToolCallRecord(name="execute_sql")],
        latency_ms=1000,
        tokens=500,
    )
    defaults.update(kw)
    return EvalScope(**defaults)


def test_output_all_pass() -> None:
    a = Assertion.model_validate(
        {"output": {"contains": ["QUERY"], "regex": r"\d rows", "not_contains": ["ERROR"]}}
    )
    results = evaluate_assertions(a, scope(), "cr1")
    assert [r.verdict for r in results] == ["pass"]


def test_output_contains_fail() -> None:
    a = Assertion.model_validate({"output": {"contains": ["MISSING"]}})
    results = evaluate_assertions(a, scope(), "cr1")
    assert results[0].verdict == "fail"
    assert "MISSING" in results[0].reason


def test_exact_mismatch() -> None:
    a = Assertion.model_validate({"output": {"exact": "other"}})
    assert evaluate_assertions(a, scope(), "cr1")[0].verdict == "fail"


def test_json_schema() -> None:
    schema = {"type": "object", "required": ["rows"]}
    ok = Assertion.model_validate({"output": {"json_schema": schema}})
    bad = Assertion.model_validate({"output": {"json_schema": {"type": "array"}}})
    assert evaluate_assertions(ok, scope(final_output='{"rows": 5}'), "cr1")[0].verdict == "pass"
    assert evaluate_assertions(bad, scope(final_output='{"rows": 5}'), "cr1")[0].verdict == "fail"
    not_json = Assertion.model_validate({"output": {"json_schema": schema}})
    assert evaluate_assertions(not_json, scope(final_output="nope"), "cr1")[0].verdict == "fail"


def test_tools_required_forbidden() -> None:
    a = Assertion.model_validate(
        {"tools": {"required": ["execute_sql"], "forbidden": ["shell_exec"]}}
    )
    assert evaluate_assertions(a, scope(), "cr1")[0].verdict == "pass"
    bad = evaluate_assertions(a, scope(tool_calls=[ToolCallRecord(name="shell_exec")]), "cr1")
    assert bad[0].verdict == "fail"
    assert "required tool not called" in bad[0].reason


def test_constraints() -> None:
    a = Assertion.model_validate(
        {"constraints": {"max_tool_calls": 1, "max_latency_ms": 100, "max_tokens": 10}}
    )
    results = evaluate_assertions(a, scope(), "cr1")
    assert results[0].verdict == "fail"
    for needle in ("max_tool_calls", "max_latency_ms", "max_tokens"):
        assert needle in results[0].reason


def test_status_extension() -> None:
    a = Assertion.model_validate({"status": "success"})
    assert evaluate_assertions(a, scope(), "cr1")[0].verdict == "pass"
    bad = evaluate_assertions(a, scope(run_status="error"), "cr1")
    assert bad[0].verdict == "fail"


def test_shape_invalid_extension_errors_on_its_own_metric() -> None:
    """形状非法的声明必须 error，且错在它自己的 metric 上——不连累别的组。"""
    a = Assertion.model_validate({"database_state": {"rows": 1}})  # 旧形状，已废弃
    results = {r.metric: r for r in evaluate_assertions(a, scope(), "cr1")}
    assert results["native.database_state"].verdict == "error"
    assert "tables_absent" in results["native.database_state"].reason  # 给出正确形状
    # 已实现的扩展有自己的 metric，native.status 不再陪跑：
    # 一条永远 pass 的指标会稀释 Gate 分母，也是"假覆盖"。
    assert "native.status" not in results


def test_unimplemented_extension_is_error_verdict() -> None:
    """词汇表内但本层不实现的键（执行型 / 依赖其他任务）→ error，不静默忽略。"""
    git = Assertion.model_validate({"git_diff": "x"})
    assert evaluate_assertions(git, scope(), "cr1")[0].verdict == "error"
    pytest_key = Assertion.model_validate({"pytest": {"tests/": {"exit_code": 0}}})
    results = {r.metric: r for r in evaluate_assertions(pytest_key, scope(), "cr1")}
    assert results["native.status"].verdict == "error"
    # 启动期报错要指向下一步动作：执行型键依赖 PRD §88 沙箱
    assert "§88" in unsupported_declarations(pytest_key, "expected")[0]


def test_removed_permission_key_is_rejected() -> None:
    """`permission` 已从词汇表移除（Spec §19.7：与 security 挂载点重复）。"""
    a = Assertion.model_validate({"permission": {"allow": ["read"]}})
    assert unsupported_declarations(a, "expected") == [
        "expected.permission: 不在断言词汇表内（Spec §2.2）"
    ]


def test_exit_code_without_observed_command_is_skipped() -> None:
    """exit_code 已实现：没有可观测的退出码时判 skipped，不是 pass 也不是 error。"""
    a = Assertion.model_validate({"exit_code": 0})
    results = {r.metric: r for r in evaluate_assertions(a, scope(), "cr1")}
    assert results["native.exit_code"].verdict == "skipped"
    assert results["native.exit_code"].blocking is False
    assert unsupported_declarations(a, "expected") == []


def test_exit_code_nonzero_command_fails() -> None:
    a = Assertion.model_validate({"exit_code": 0})
    bad = scope(command_calls=[ToolCallRecord(name="rm", exit_code=1)])
    results = {r.metric: r for r in evaluate_assertions(a, bad, "cr1")}
    assert results["native.exit_code"].verdict == "fail"
    good = scope(command_calls=[ToolCallRecord(name="ls", exit_code=0)])
    assert evaluate_assertions(a, good, "cr1")[0].verdict == "pass"


def test_max_cost_without_pricing_is_skipped() -> None:
    """PRD §59：无定价时 cost 为 None。判 skipped 而不是"0 <= max_cost → pass"。"""
    a = Assertion.model_validate({"constraints": {"max_cost": 0.001}})
    results = {r.metric: r for r in evaluate_assertions(a, scope(), "cr1")}
    assert results["native.performance"].verdict == "skipped"
    assert unsupported_declarations(a, "expected") == []
    # 有定价时才真判
    over = scope(cost=0.5)
    assert evaluate_assertions(a, over, "cr1")[0].verdict == "fail"
    under = scope(cost=0.0001)
    assert evaluate_assertions(a, under, "cr1")[0].verdict == "pass"


def test_supported_declarations_have_no_problems() -> None:
    a = Assertion.model_validate(
        {"status": "success", "constraints": {"max_tool_calls": 5, "max_tokens": 10}}
    )
    assert unsupported_declarations(a, "expected") == []


def test_empty_assertion_yields_no_results() -> None:
    assert evaluate_assertions(Assertion(), scope(), "cr1") == []


def test_turn_index_metadata() -> None:
    a = Assertion.model_validate({"output": {"contains": ["QUERY"]}})
    results = evaluate_assertions(a, scope(), "cr1", turn_index=2)
    assert results[0].metadata == {"turn": 2}


def test_mount_metadata() -> None:
    a = Assertion.model_validate({"output": {"contains": ["QUERY"]}})
    results = evaluate_assertions(a, scope(), "cr1", mount="expected.final")
    assert results[0].metadata == {"mount": "expected.final"}


def test_platform_verdict_synthesized_for_abnormal_end() -> None:
    """未正常结束的执行必须产生平台级 FAIL，不依赖 Case 是否声明 status。"""
    assert synthesize_platform_verdicts(scope(), "cr1") == []
    verdicts = synthesize_platform_verdicts(scope(run_status="timeout"), "cr1")
    assert [(m.metric, m.verdict, m.blocking) for m in verdicts] == [
        ("native.status", "fail", True)
    ]


def test_missing_observation_is_not_scored() -> None:
    """约束只评估真正观测到的量（latency/tokens 之外的量不得凭空判定）。"""
    a = Assertion.model_validate({"constraints": {"max_latency_ms": 5000}})
    assert evaluate_assertions(a, scope(), "cr1")[0].verdict == "pass"


def test_unknown_extension_reported_as_outside_vocabulary() -> None:
    a = Assertion.model_validate({"totally_unknown": 1})
    assert unsupported_declarations(a, "expected") == [
        "expected.totally_unknown: 不在断言词汇表内（Spec §2.2）"
    ]
    # 评测期同样必须报错，而不是静默忽略一个看不懂的声明
    assert evaluate_assertions(a, scope(), "cr1")[0].verdict == "error"


class TestEnvironmentAssertions:
    """database_state / file_state（Spec §19.2/§19.3）：判定对象是环境，不是输出。

    两类断言都有三种结局，缺一不可：满足 → pass、不满足 → fail、
    **观测不到 → skipped**。只有前两种时，"fixture 没给数据库"会被静默算成
    通过（假信号）或算成失败（冤枉 agent），两者都污染 Gate。
    """

    def _snapshot(self, tmp_path, *, seed: str | None = None):
        import sqlite3

        from agent_eval.fixtures.snapshot import EnvironmentSnapshot

        workdir = tmp_path / "ws"
        workdir.mkdir()
        db_path = tmp_path / "fixture.db"
        conn = sqlite3.connect(db_path)
        if seed is not None:
            conn.executescript(seed)
        else:
            conn.close()
            db_path.unlink()
            return EnvironmentSnapshot(db_path=db_path, workdir=workdir)
        conn.commit()
        conn.close()
        return EnvironmentSnapshot(db_path=db_path, workdir=workdir)

    def test_table_rows_within_bounds_pass(self, tmp_path) -> None:
        env = self._snapshot(
            tmp_path, seed="CREATE TABLE orders (id INTEGER); INSERT INTO orders VALUES (1),(2);"
        )
        a = Assertion.model_validate({"database_state": {"tables": {"orders": {"min_rows": 2}}}})
        assert evaluate_assertions(a, scope(environment=env), "cr1")[0].verdict == "pass"

    def test_table_rows_out_of_bounds_fail(self, tmp_path) -> None:
        env = self._snapshot(
            tmp_path, seed="CREATE TABLE orders (id INTEGER); INSERT INTO orders VALUES (1),(2);"
        )
        a = Assertion.model_validate({"database_state": {"tables": {"orders": {"max_rows": 1}}}})
        results = evaluate_assertions(a, scope(environment=env), "cr1")
        assert results[0].verdict == "fail"
        assert "max_rows 1" in results[0].reason

    def test_absent_table_expectations(self, tmp_path) -> None:
        env = self._snapshot(tmp_path, seed="CREATE TABLE kept (id INTEGER);")
        ok = Assertion.model_validate({"database_state": {"tables_absent": ["dropped"]}})
        assert evaluate_assertions(ok, scope(environment=env), "cr1")[0].verdict == "pass"
        bad = Assertion.model_validate({"database_state": {"tables_absent": ["kept"]}})
        assert evaluate_assertions(bad, scope(environment=env), "cr1")[0].verdict == "fail"

    def test_no_snapshot_is_skipped(self) -> None:
        a = Assertion.model_validate({"database_state": {"tables": {"orders": {"min_rows": 1}}}})
        results = evaluate_assertions(a, scope(), "cr1")
        assert results[0].verdict == "skipped"
        assert results[0].blocking is False
        assert "没有可读的环境快照" in results[0].reason

    def test_no_database_is_skipped(self, tmp_path) -> None:
        env = self._snapshot(tmp_path)  # filesystem fixture：没有库文件
        a = Assertion.model_validate({"database_state": {"tables": {"orders": {"min_rows": 1}}}})
        results = evaluate_assertions(a, scope(environment=env), "cr1")
        assert results[0].verdict == "skipped"

    def test_file_state_contains_and_absent(self, tmp_path) -> None:
        env = self._snapshot(tmp_path)
        (env.workdir / "report.md").write_text("rows: 5\n", encoding="utf-8")
        ok = Assertion.model_validate(
            {"file_state": {"files": {"report.md": {"contains": ["rows: 5"]}}, "absent": ["tmp"]}}
        )
        assert evaluate_assertions(ok, scope(environment=env), "cr1")[0].verdict == "pass"
        bad = Assertion.model_validate(
            {"file_state": {"files": {"report.md": {"not_contains": ["rows: 5"]}}}}
        )
        assert evaluate_assertions(bad, scope(environment=env), "cr1")[0].verdict == "fail"

    def test_file_state_escaping_workdir_is_an_error(self, tmp_path) -> None:
        """`../` 是**声明写错**，判 error——不读越界文件，也不伪装成"文件不存在"。

        伪装成"文件不存在"会让用例作者去查 agent 为什么没产出文件，
        而真正的问题在用例自己的路径上（Spec §19.3）。
        """
        env = self._snapshot(tmp_path)
        (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
        a = Assertion.model_validate({"file_state": {"files": {"../secret.txt": {}}}})
        results = {r.metric: r for r in evaluate_assertions(a, scope(environment=env), "cr1")}
        assert results["native.file_state"].verdict == "error"
        assert "越出 fixture workdir" in results["native.file_state"].reason
        # 启动期同样要拦下，别等到跑完一轮才发现
        assert any("越出 fixture workdir" in p for p in unsupported_declarations(a, "expected"))

    def test_illegal_table_name_fails_fast(self) -> None:
        a = Assertion.model_validate(
            {"database_state": {"tables": {"orders; DROP TABLE customers": {"exists": True}}}}
        )
        problems = unsupported_declarations(a, "expected")
        assert any("非法表名" in p for p in problems)


class TestSqlResultAssertion:
    """sql_result（Spec §19.5）：判定 SQL 工具的返回结果，不是输出文本。"""

    def test_row_count_both_directions(self) -> None:
        calls = [ToolCallRecord(name="execute_sql", result=[{"id": 1}, {"id": 2}])]
        a = Assertion.model_validate({"sql_result": {"min_rows": 2, "max_rows": 2}})
        assert evaluate_assertions(a, scope(tool_calls=calls), "cr1")[0].verdict == "pass"
        too_many = Assertion.model_validate({"sql_result": {"max_rows": 1}})
        assert evaluate_assertions(too_many, scope(tool_calls=calls), "cr1")[0].verdict == "fail"

    def test_dict_shaped_result_is_understood(self) -> None:
        calls = [ToolCallRecord(name="execute_sql", result={"rows": [1, 2, 3]})]
        a = Assertion.model_validate({"sql_result": {"min_rows": 3}})
        assert evaluate_assertions(a, scope(tool_calls=calls), "cr1")[0].verdict == "pass"

    def test_contains_reads_result_not_output(self) -> None:
        calls = [ToolCallRecord(name="execute_sql", result={"rows": [{"name": "acmeCorp"}]})]
        a = Assertion.model_validate({"sql_result": {"contains": ["acmeCorp"]}})
        hit = evaluate_assertions(a, scope(tool_calls=calls, final_output="无"), "cr1")
        assert hit[0].verdict == "pass"
        miss = Assertion.model_validate({"sql_result": {"contains": ["globex"]}})
        assert evaluate_assertions(miss, scope(tool_calls=calls), "cr1")[0].verdict == "fail"

    def test_no_sql_call_is_skipped(self) -> None:
        a = Assertion.model_validate({"sql_result": {"min_rows": 1}})
        results = evaluate_assertions(a, scope(), "cr1")
        assert results[0].verdict == "skipped"
        assert results[0].blocking is False

    def test_unknown_result_shape_is_skipped(self) -> None:
        """认不出行数时判 skipped：硬凑一个 0 会把"看不到"说成"没数据"。"""
        calls = [ToolCallRecord(name="execute_sql", result="ok")]
        a = Assertion.model_validate({"sql_result": {"min_rows": 1}})
        results = evaluate_assertions(a, scope(tool_calls=calls), "cr1")
        assert results[0].verdict == "skipped"


class TestEmptyExtensionBlocks:
    """空块不产出 metric：`sql_result: {}` 若产出，就是一条永远 pass 的指标。

    口径与 `output: {}` / `tools: {}` 一致（不声明检查的组不产出）。
    唯一例外是 `exit_code: {}`——它的缺省语义（全部命令以 0 结束）是条真断言。
    """

    def test_empty_blocks_produce_no_metric(self) -> None:
        a = Assertion.model_validate({"sql_result": {}, "database_state": {}, "file_state": {}})
        assert evaluate_assertions(a, scope(), "cr1") == []

    def test_exit_code_empty_block_is_a_real_assertion(self) -> None:
        a = Assertion.model_validate({"exit_code": {}})
        good = scope(command_calls=[ToolCallRecord(name="ls", exit_code=0)])
        results = {r.metric: r for r in evaluate_assertions(a, good, "cr1")}
        assert results["native.exit_code"].verdict == "pass"
        bad = scope(command_calls=[ToolCallRecord(name="ls", exit_code=1)])
        assert evaluate_assertions(a, bad, "cr1")[0].verdict == "fail"

    def test_empty_block_is_not_reported_as_unsupported(self) -> None:
        """空块只是不判，不是配置错误：`case validate` 不该为它报警。"""
        a = Assertion.model_validate({"sql_result": {}})
        assert unsupported_declarations(a, "expected") == []
