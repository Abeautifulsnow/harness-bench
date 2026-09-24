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


def test_unsupported_extension_is_error_verdict() -> None:
    a = Assertion.model_validate({"database_state": {"rows": 1}})  # 落入 extensions
    results = evaluate_assertions(a, scope(), "cr1")
    assert results[0].verdict == "error"
    git = Assertion.model_validate({"git_diff": "x"})
    assert evaluate_assertions(git, scope(), "cr1")[0].verdict == "error"


def test_exit_code_declaration_is_reported_unsupported() -> None:
    """exit_code 在词汇表内但无观测来源 → 必须报为不可评测，不得静默恒 fail。"""
    a = Assertion.model_validate({"exit_code": 0})
    assert evaluate_assertions(a, scope(), "cr1")[0].verdict == "error"
    assert unsupported_declarations(a, "expected") == ["expected.exit_code: 在词汇表内但尚未实现"]


def test_max_cost_declaration_is_reported_unsupported() -> None:
    """max_cost 同理：未接入定价前声明它不能变成一条永不失败的断言。"""
    a = Assertion.model_validate({"constraints": {"max_cost": 0.001}})
    assert evaluate_assertions(a, scope(), "cr1")[0].verdict == "error"
    assert any("max_cost" in problem for problem in unsupported_declarations(a, "expected"))


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
