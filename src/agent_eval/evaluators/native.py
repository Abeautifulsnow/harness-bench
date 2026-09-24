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
from agent_eval.models.case import (
    EXTENSION_KEYS,
    Assertion,
    StepEfficiencyAssertion,
    ToolArgumentMatcher,
    argument_path,
)
from agent_eval.models.results import MetricResultModel, ToolCallRecord

# Case 级扩展断言中本层已实现的部分。
# 未列出的词汇（无论是否在 Spec 词汇表内）一律 fail-fast，而不是静默接受后失效：
# 声明了却永不生效的断言与"永不失败的断言"都会污染 Gate 结论。
IMPLEMENTED_EXTENSIONS = {"status", "tool_arguments", "step_efficiency"}
# Spec §2.2 词汇表内、但尚未实现的扩展（观察量无来源或未定义语义）
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
                f"assertion extension '{key}' is outside the implemented surface"
            )
    if "status" in assertion.extensions:
        expected_status = str(assertion.extensions["status"])
        if scope.run_status != expected_status:
            problems.append(f"run status {scope.run_status!r} != expected {expected_status!r}")
    return problems


def parse_tool_arguments(assertion: Assertion) -> dict[str, dict[str, ToolArgumentMatcher]] | None:
    """解析 ``tool_arguments`` 扩展（Spec V2.2 §11.2）；形状非法 → None（由调用方判 error）。"""
    raw = assertion.extensions.get("tool_arguments")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, dict[str, ToolArgumentMatcher]] = {}
    for tool, paths in raw.items():
        if not isinstance(paths, dict):
            return None
        try:
            parsed[str(tool)] = {
                str(path): ToolArgumentMatcher.model_validate(matcher or {})
                for path, matcher in paths.items()
            }
        except Exception:
            return None
    return parsed


def _check_tool_arguments(assertion: Assertion, scope: EvalScope) -> list[str]:
    """逐 required tool 比对已声明参数子集（PRD §56/§57 的确定性版本）。"""
    parsed = parse_tool_arguments(assertion)
    if parsed is None:
        raise UnsupportedAssertionError(
            "tool_arguments must be a mapping {tool: {arg_path: matcher}} (Spec V2.2 §11.2)"
        )
    calls_by_tool: dict[str, list[ToolCallRecord]] = {}
    for call in scope.tool_calls:
        calls_by_tool.setdefault(call.name, []).append(call)
    problems: list[str] = []
    for tool, paths in parsed.items():
        calls = calls_by_tool.get(tool)
        if not calls:
            problems.append(f"tool_arguments declared for '{tool}' but it was never called")
            continue
        for path, matcher in paths.items():
            matched = False
            first_reason = ""
            for call in calls:
                present, value = argument_path(call.arguments, path)
                if not present:
                    first_reason = first_reason or f"path '{path}' missing"
                    continue
                reason = matcher.check(value)
                if reason is None:
                    matched = True
                    break
                first_reason = first_reason or reason
            if not matched:
                problems.append(f"tool '{tool}' argument {path}: {first_reason}")
    return problems


def parse_step_efficiency(assertion: Assertion) -> StepEfficiencyAssertion | None:
    """解析 ``step_efficiency`` 扩展；非法形状 → None（由调用方按 error 处理）。"""
    raw = assertion.extensions.get("step_efficiency")
    if raw is None:
        return None
    try:
        return StepEfficiencyAssertion.model_validate(raw)
    except Exception:
        return None


def _check_step_ratio(assertion: Assertion, scope: EvalScope) -> list[str]:
    """``native.step_ratio``（Spec V2.2 §11.1）：实际步数不得超过声明基线。"""
    spec = parse_step_efficiency(assertion)
    if spec is None:
        raise UnsupportedAssertionError(
            "step_efficiency must be a mapping {baseline_steps: int, max_ratio_delta?: float} "
            "(Spec V2.2 §11.1)"
        )
    actual = len(scope.tool_calls)
    limit = spec.limit()
    if actual <= limit:
        return []
    return [
        f"step ratio {spec.baseline_steps}/{actual} = "
        f"{step_ratio(assertion, scope):.3f} < 1.0（基线 {spec.baseline_steps}，上限 {limit:g}）"
    ]


def step_ratio(assertion: Assertion, scope: EvalScope) -> float:
    """步数效率得分：``min(1, baseline/actual)``（PRD §31 step efficiency 口径）。"""
    spec = parse_step_efficiency(assertion)
    baseline = spec.baseline_steps if spec is not None else 0
    actual = len(scope.tool_calls)
    if actual == 0:
        return 1.0 if baseline == 0 else 0.0
    return round(min(1.0, baseline / actual), 6)


def _has_extensions(assertion: Assertion) -> bool:
    for key in assertion.extensions:
        if key not in IMPLEMENTED_EXTENSIONS:
            raise UnsupportedAssertionError(
                f"assertion extension '{key}' is outside the implemented surface"
            )
    return bool(assertion.extensions)


def unsupported_declarations(assertion: Assertion, mount: str) -> list[str]:
    """列出该挂载点上无法评测的声明（启动期 fail-fast 的输入，Spec §2.2）。

    区分两类，便于报错信息可执行：
      - 词汇表内但未实现（如 exit_code / max_cost）→ 明确说明"未实现"
      - 完全不在词汇表内 → 说明违反 Spec §2.2
    """
    problems: list[str] = []
    for key in assertion.extensions:
        if key in IMPLEMENTED_EXTENSIONS:
            continue
        if key in KNOWN_EXTENSIONS:
            problems.append(f"{mount}.{key}: 在词汇表内但尚未实现")
        else:
            problems.append(f"{mount}.{key}: 不在断言词汇表内（Spec §2.2）")
    if parse_tool_arguments(assertion) is None and "tool_arguments" in assertion.extensions:
        problems.append(f"{mount}.tool_arguments: 形状非法，应为 {{tool: {{arg_path: matcher}}}}")
    if parse_step_efficiency(assertion) is None and "step_efficiency" in assertion.extensions:
        problems.append(
            f"{mount}.step_efficiency: 形状非法，应为 "
            "{baseline_steps: int, max_ratio_delta?: float}"
        )
    if assertion.constraints.max_cost is not None:
        problems.append(f"{mount}.constraints.max_cost: 尚未实现（PRD §59 cost 属 P2）")
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
    ("native.argument_checks", lambda a: "tool_arguments" in a.extensions, _check_tool_arguments),
    ("native.performance", lambda a: not a.constraints.is_empty(), _check_constraints),
    ("native.step_ratio", lambda a: "step_efficiency" in a.extensions, _check_step_ratio),
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
        score = _group_score(metric_id, assertion, scope, not problems)
        results.append(
            _result(
                case_run_id,
                metric_id,
                verdict,
                "all checks passed" if not problems else "; ".join(problems),
                score=score,
                metadata=dict(extra_meta),
            )
        )
    return results


def _group_score(metric_id: str, assertion: Assertion, scope: EvalScope, passed: bool) -> float:
    """组得分：step_ratio 上报连续分（供 agent.step_efficiency 降级与趋势使用），其余 0/1。"""
    if metric_id == "native.step_ratio":
        return step_ratio(assertion, scope)
    return 1.0 if passed else 0.0
