"""Native Evaluator：确定性规则优先（PRD §34 + Spec §2.2 统一断言词汇表）。

输入是单次执行（iteration / 或单个 turn）的观测切片 EvalScope，
输出一组 MetricResult（组粒度：status / output / tools / performance）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate

from agent_eval.errors import UnsupportedAssertionError
from agent_eval.ids import new_id
from agent_eval.models.case import EXTENSION_KEYS, Assertion
from agent_eval.models.results import MetricResultModel, ToolCallRecord

# P0 实际实现了的 Case 级扩展断言。
# 未列出的词汇（无论是否在 Spec 词汇表内）一律 fail-fast，而不是静默接受后失效：
# 声明了却永不生效的断言与"永不失败的断言"都会污染 Gate 结论。
IMPLEMENTED_EXTENSIONS = {"status"}
# Spec §2.2 词汇表内、但 P0 尚未实现的扩展（观察量在 P0 无来源）
KNOWN_EXTENSIONS = EXTENSION_KEYS - IMPLEMENTED_EXTENSIONS


@dataclass
class EvalScope:
    """一次被评测执行的观测切片。

    只放 P0 真正能观测到的量；没有来源的字段（cost、exit code）不设默认值占位，
    否则会变成"永远判定为 pass/fail"的假信号。
    """

    run_status: str  # success | error | timeout
    final_output: str | None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    latency_ms: int = 0
    tokens: int = 0


def _result(
    case_run_id: str,
    metric: str,
    verdict: str,
    reason: str,
    *,
    score: float | None = None,
    metadata: dict[str, Any] | None = None,
    blocking: bool = True,
) -> MetricResultModel:
    return MetricResultModel(
        id=new_id("mr"),
        case_run_id=case_run_id,
        metric=metric,
        evaluator="native",
        score=score,
        verdict=verdict,  # type: ignore[arg-type]
        blocking=blocking,
        reason=reason,
        metadata=metadata or {},
    )


def _check_output(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    output = assertion.output
    if output.is_empty():
        return problems
    text = scope.final_output or ""
    if output.exact is not None and text != output.exact:
        problems.append(f"exact mismatch: got {text[:120]!r}")
    for needle in output.contains:
        if needle not in text:
            problems.append(f"missing expected substring {needle!r}")
    for needle in output.not_contains:
        if needle in text:
            problems.append(f"forbidden substring {needle!r} present")
    if output.regex is not None and re.search(output.regex, text) is None:
        problems.append(f"regex {output.regex!r} did not match")
    if output.json_schema is not None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            problems.append("output is not valid JSON (json_schema assertion)")
        else:
            try:
                jsonschema_validate(parsed, output.json_schema)
            except ValidationError as exc:
                problems.append(f"json_schema validation failed: {exc.message}")
    return problems


def _check_tools(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    tools = assertion.tools
    if tools.is_empty():
        return problems
    called = [t.name for t in scope.tool_calls]
    for required in tools.required:
        if required not in called:
            problems.append(f"required tool not called: {required}")
    for forbidden in tools.forbidden:
        if forbidden in called:
            problems.append(f"forbidden tool called: {forbidden}")
    return problems


def _check_constraints(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    limits = assertion.constraints
    if limits.is_empty():
        return problems
    if limits.max_cost is not None:
        raise UnsupportedAssertionError(
            "constraints.max_cost is not implemented by the P0 native evaluator "
            "(PRD §59 cost 属 P2；P0 不计算成本，无法观测)"
        )
    if limits.max_tool_calls is not None and len(scope.tool_calls) > limits.max_tool_calls:
        problems.append(
            f"tool calls {len(scope.tool_calls)} > max_tool_calls {limits.max_tool_calls}"
        )
    if limits.max_latency_ms is not None and scope.latency_ms > limits.max_latency_ms:
        problems.append(f"latency {scope.latency_ms}ms > max_latency_ms {limits.max_latency_ms}")
    if limits.max_tokens is not None and scope.tokens > limits.max_tokens:
        problems.append(f"tokens {scope.tokens} > max_tokens {limits.max_tokens}")
    return problems


def _check_extensions(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    for key in assertion.extensions:
        if key not in IMPLEMENTED_EXTENSIONS:
            raise UnsupportedAssertionError(
                f"assertion extension '{key}' is outside the P0 implementation surface"
            )
    if "status" in assertion.extensions:
        expected_status = str(assertion.extensions["status"])
        if scope.run_status != expected_status:
            problems.append(f"run status {scope.run_status!r} != expected {expected_status!r}")
    return problems


def _has_extensions(assertion: Assertion) -> bool:
    for key in assertion.extensions:
        if key not in IMPLEMENTED_EXTENSIONS:
            raise UnsupportedAssertionError(
                f"assertion extension '{key}' is outside the P0 implementation surface"
            )
    return bool(assertion.extensions)


def unsupported_declarations(assertion: Assertion, mount: str) -> list[str]:
    """列出该挂载点上 P0 无法评测的声明（启动期 fail-fast 的输入，Spec §2.2/§96）。

    区分两类，便于报错信息可执行：
      - 词汇表内但 P0 未实现（如 exit_code / max_cost）→ 明确说明"未实现"
      - 完全不在词汇表内 → 说明违反 Spec §2.2
    """
    problems: list[str] = []
    for key in assertion.extensions:
        if key in IMPLEMENTED_EXTENSIONS:
            continue
        if key in KNOWN_EXTENSIONS:
            problems.append(f"{mount}.{key}: 在词汇表内但超出 P0 实现面")
        else:
            problems.append(f"{mount}.{key}: 不在断言词汇表内（Spec §2.2）")
    if assertion.constraints.max_cost is not None:
        problems.append(f"{mount}.constraints.max_cost: 超出 P0 实现面（PRD §59 cost 属 P2）")
    return problems


def synthesize_platform_verdicts(scope: EvalScope, case_run_id: str) -> list[MetricResultModel]:
    """平台级判定（非 Case 声明）：未正常结束的 run 不得判 PASS。

    放在评测层而不是编排层：这是评测策略，编排层只负责驱动。
    """
    if scope.run_status == "success":
        return []
    return [
        _result(
            case_run_id,
            "native.status",
            "fail",
            f"agent run finished with status={scope.run_status!r}",
            metadata={"platform": True},
        )
    ]


_GROUPS = (
    ("native.status", _has_extensions, _check_extensions),
    ("native.output_checks", lambda a: not a.output.is_empty(), _check_output),
    ("native.tool_sequence", lambda a: not a.tools.is_empty(), _check_tools),
    ("native.performance", lambda a: not a.constraints.is_empty(), _check_constraints),
)


def evaluate_assertions(
    assertion: Assertion,
    scope: EvalScope,
    case_run_id: str,
    *,
    turn_index: int | None = None,
    mount: str | None = None,
) -> list[MetricResultModel]:
    """All checks inside a mount point combine with AND (Spec §2.2).

    每个声明了检查的组产出一条 MetricResult（组内全过 → pass）；
    未声明任何检查的组不产出，避免空断言刷分。
    """
    results: list[MetricResultModel] = []
    extra_meta: dict[str, Any] = {}
    if turn_index is not None:
        extra_meta["turn"] = turn_index
    if mount is not None:
        extra_meta["mount"] = mount
    for metric_id, declared, checker in _GROUPS:
        try:
            if not declared(assertion):
                continue
            problems = checker(assertion, scope)
        except UnsupportedAssertionError as exc:
            results.append(
                _result(case_run_id, metric_id, "error", str(exc), metadata=dict(extra_meta))
            )
            continue
        verdict = "pass" if not problems else "fail"
        results.append(
            _result(
                case_run_id,
                metric_id,
                verdict,
                "all checks passed" if not problems else "; ".join(problems),
                score=1.0 if not problems else 0.0,
                metadata=dict(extra_meta),
            )
        )
    return results
