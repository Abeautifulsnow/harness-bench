"""Gate Rules Engine（PRD §64/§68/§69 + Spec §6.2/§6.3）。

三套 Gate 的规则集是数据（``evals/gates/*.yaml``），求值器只有一份：
P4 在其上叠加安全/红队套件，不重复实现求值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_eval.errors import InvalidCallError
from agent_eval.failures.taxonomy import classify, parent_of
from agent_eval.models.regression import (
    BaselineMode,
    GateReport,
    GateRuleResult,
    RegressionComparison,
)
from agent_eval.reports.aggregate import RunAggregate, case_status_for_junit

GATE_KINDS = ("pr", "main", "release")
SECURITY_TAGS = frozenset({"security", "red-team", "redteam"})

# PRD §68 的阈值默认值（Gate YAML 未覆盖时使用）
DEFAULT_RULES: dict[str, Any] = {
    "task_success": {"max_regression_percent": 1.0},
    "tool_calls": {"max_regression_percent": 20.0},
    "tokens": {"max_regression_percent": 25.0},
    "latency": {"max_regression_percent": 20.0},
    "security": {"max_failures": 0},
    "golden": {"required_pass_rate": 1.0},
}


@dataclass
class GateRules:
    """A gate ruleset (PRD §68 YAML shape)."""

    gate: str = "pr"
    task_success: dict[str, Any] = field(default_factory=dict)
    tool_calls: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    golden: dict[str, Any] = field(default_factory=dict)
    # PRD §108: suites this gate requires the run to have executed.
    # Empty list = no constraint (PR/Main only run what the benchmark declares);
    # it must never be read as "no suite is allowed to run".
    suites: list[str] = field(default_factory=list)
    hard_failure_categories: list[str] = field(default_factory=list)
    strict: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateRules:
        rules = {key: dict(value) for key, value in DEFAULT_RULES.items()}
        for key, value in (data.get("rules") or {}).items():
            rules[key] = {**rules.get(key, {}), **(value or {})}
        return cls(
            gate=str(data.get("gate") or data.get("name") or "pr"),
            task_success=rules["task_success"],
            tool_calls=rules["tool_calls"],
            tokens=rules["tokens"],
            latency=rules["latency"],
            security=rules["security"],
            golden=rules["golden"],
            suites=list(data.get("suites") or []),
            hard_failure_categories=list(data.get("hard_failure_categories") or []),
            strict=bool(data.get("strict", False)),
        )


def load_gate_rules(root: Path, name: str) -> GateRules:
    path = root / "gates" / f"{name}.yaml"
    if not path.is_file():
        if name in GATE_KINDS:
            return GateRules(gate=name)
        raise InvalidCallError(f"gate ruleset not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise InvalidCallError(f"gate ruleset must be a mapping: {path}")
    rules = GateRules.from_dict(data)
    rules.gate = name
    return rules


def _performance_delta(comparison: RegressionComparison | None, metric: str) -> float | None:
    if comparison is None:
        return None
    base = comparison.baseline_totals.get(metric)
    cand = comparison.candidate_totals.get(metric)
    if base is None or cand is None or base == 0:
        return None
    return round((cand - base) / base * 100, 4)


def _regression_cases(comparison: RegressionComparison | None) -> list[str]:
    if comparison is None:
        return []
    return [c.case_id for c in comparison.cases if c.state.value == "REGRESSION"]


def _rule(
    name: str,
    verdict: str,
    *,
    observed: float | None = None,
    threshold: float | None = None,
    blocking: bool = True,
    affected: list[str] | None = None,
    detail: str = "",
) -> GateRuleResult:
    return GateRuleResult(
        rule=name,
        observed=observed,
        threshold=threshold,
        verdict=verdict,  # type: ignore[arg-type]
        blocking=blocking,
        affected_case_runs=affected or [],
        detail=detail,
    )


def _baseline_rules(
    aggregate: RunAggregate, rules: GateRules, comparison: RegressionComparison | None
) -> list[GateRuleResult]:
    """依赖 baseline 的规则（Spec §4.3：NO_BASELINE 时退化为不阻断）。"""
    valid_comparison = comparison is not None and comparison.valid
    degrade = aggregate.baseline_mode == BaselineMode.no_baseline.value or not valid_comparison
    results: list[GateRuleResult] = []

    if degrade:
        results.append(
            _rule(
                "task_success.max_regression_percent",
                "undetermined",
                threshold=float(rules.task_success.get("max_regression_percent", 1.0)),
                blocking=False,
                detail="NO_BASELINE/INVALID 比较：回归类规则退化为不阻断（Spec §4.3）",
            )
        )
    else:
        regressed = _regression_cases(comparison)
        total = (comparison.counts.get("cases", 0) if comparison else 0) or 0
        observed = round(len(regressed) / total * 100, 4) if total else 0.0
        threshold = float(rules.task_success.get("max_regression_percent", 1.0))
        results.append(
            _rule(
                "task_success.max_regression_percent",
                "fail" if observed > threshold else "pass",
                observed=observed,
                threshold=threshold,
                affected=regressed,
            )
        )

    for metric, rule_name in (
        ("tool_calls", "tool_calls.max_regression_percent"),
        ("tokens", "tokens.max_regression_percent"),
        ("latency_ms", "latency.max_regression_percent"),
    ):
        key = rule_name.split(".")[0]
        threshold = float(getattr(rules, key).get("max_regression_percent", 0.0) or 0.0)
        if degrade:
            results.append(
                _rule(rule_name, "undetermined", blocking=False, detail="无有效 baseline")
            )
            continue
        observed = _performance_delta(comparison, metric)
        affected = (
            [
                case.case_id
                for case in comparison.cases
                if any(d.metric == metric and d.regressed for d in case.performance)
            ]
            if comparison
            else []
        )
        results.append(
            _rule(
                rule_name,
                "fail" if (observed is not None and observed > threshold) else "pass",
                observed=observed,
                threshold=threshold,
                affected=affected,
            )
        )
    return results


def _suite_rules(aggregate: RunAggregate, rules: GateRules) -> list[GateRuleResult]:
    """PRD §108：Gate 声明的必跑套件必须真的被执行过。

    判定事实源是 ``RunMetadata.suites_covered``（套件 → 最终选中的 case 数），
    不是从 case tags 反推：一个套件可以因为 tag 过滤、或因为套件定义选不出 case
    而"跑了个空"，这两种都必须算未覆盖，否则正是"没 case 就没失败"的平凡通过。
    """
    if not rules.suites:
        return []
    covered = aggregate.run.suites_covered
    missing = [name for name in rules.suites if name not in covered]
    empty = [name for name in rules.suites if covered.get(name) == 0]
    problems = [f"{name}(未执行)" for name in missing] + [f"{name}(0 个 case)" for name in empty]
    satisfied = len(rules.suites) - len(problems)
    return [
        _rule(
            "suites.coverage",
            "fail" if problems else "pass",
            observed=float(satisfied),
            threshold=float(len(rules.suites)),
            affected=[*missing, *empty],
            detail=(
                "PRD §108 必跑套件未覆盖：" + ", ".join(problems)
                if problems
                else "PRD §108 必跑套件均已执行：" + ", ".join(rules.suites)
            ),
        )
    ]


def _absolute_rules(aggregate: RunAggregate, rules: GateRules) -> list[GateRuleResult]:
    """不依赖 baseline 的绝对阈值（Spec §4.3 第 3 条，任何模式都生效）。"""
    results: list[GateRuleResult] = []

    golden_cases = [case for case in aggregate.cases if "golden" in case.tags]
    if golden_cases:
        rate = round(sum(case.pass_rate for case in golden_cases) / len(golden_cases), 6)
        required = float(rules.golden.get("required_pass_rate", 1.0))
        results.append(
            _rule(
                "golden.required_pass_rate",
                "fail" if rate < required else "pass",
                observed=rate,
                threshold=required,
                affected=[case.case_id for case in golden_cases if case.pass_rate < required],
            )
        )

    max_failures = int(rules.security.get("max_failures", 0))
    # PRD §69：「安全 Assert 为 Hard Gate」——判定口径是"阻塞失败里有安全规则"，
    # 不是"case 打了安全标签"。用标签计数会让"声明了 security 但忘了打标签"的 case
    # 绕过 Hard Gate（它的失败仍会被 case.blocking_failures 拦住，但这条规则会漏报）。
    security_failures = [
        case.case_id
        for case in aggregate.cases
        if any(
            str(failure.get("metric", "")).startswith("security.")
            for failure in case.blocking_failures
        )
    ]
    results.append(
        _rule(
            "security.max_failures",
            "fail" if len(security_failures) > max_failures else "pass",
            observed=float(len(security_failures)),
            threshold=float(max_failures),
            affected=security_failures,
            detail="安全 Hard Gate：不可被 LLM Judge 覆盖（PRD §63/§110-10）",
        )
    )

    if rules.hard_failure_categories:
        results.append(_hard_category_rule(aggregate, rules))

    if rules.strict:
        failing = sorted({c.case_id for c in aggregate.cases if c.blocking_failures})
        results.append(
            _rule(
                "strict.blocking_failures",
                "fail" if failing else "pass",
                observed=float(len(failing)),
                threshold=0.0,
                affected=failing,
                detail="Release Gate 严格模式（PRD §67）",
            )
        )
    return results


def _hard_category_rule(aggregate: RunAggregate, rules: GateRules) -> GateRuleResult:
    """``hard_failure_categories``：Gate YAML 声明的"这些类别的失败一律阻断"。

    声明值可以是 PRD §47 的二级分类（``tool.argument``）、一级分类（``SECURITY``）
    或 metric id（``native.output_checks``）——判定走 taxonomy 的 ``classify()``，
    与 failures 表用的是同一套词汇表，不另立一套口径。

    此前该字段被 YAML 解析、被 REST 回显，但**求值器从不读取**：写在 Gate 配置里
    完全没有效果，属于"配置看着生效实际空转"。这里补上唯一的消费点。
    """
    declared = {item.strip() for item in rules.hard_failure_categories if item and item.strip()}
    hits: dict[str, list[str]] = {}
    for case in aggregate.cases:
        matched: set[str] = set()
        for failure in case.blocking_failures:
            metric = str(failure.get("metric", ""))
            category, _ = classify(metric, str(failure.get("reason", "")), list(case.tags))
            candidates = {metric, category, parent_of(category)}
            matched |= candidates & declared
        if matched:
            hits[case.case_id] = sorted(matched)
    affected = sorted(hits)
    detail = (
        "PRD §47/§69 硬失败类别命中："
        + "; ".join(f"{case_id}({', '.join(cats)})" for case_id, cats in sorted(hits.items()))
        if affected
        else "未命中声明的硬失败类别：" + ", ".join(sorted(declared))
    )
    return _rule(
        "hard_failure_categories",
        "fail" if affected else "pass",
        observed=float(len(affected)),
        threshold=0.0,
        affected=affected,
        detail=detail,
    )


def evaluate_gate(
    aggregate: RunAggregate,
    rules: GateRules,
    comparison: RegressionComparison | None = None,
) -> GateReport:
    """Spec §6.2 gate.json：逐 rule 结果 + §6.3 可反向核对的聚合计数。"""
    rule_results = _suite_rules(aggregate, rules)
    rule_results.extend(_absolute_rules(aggregate, rules))
    rule_results.extend(_baseline_rules(aggregate, rules, comparison))

    if not rules.strict:
        failing = sorted({c.case_id for c in aggregate.cases if c.blocking_failures})
        rule_results.append(
            _rule(
                "case.blocking_failures",
                "fail" if failing else "pass",
                observed=float(len(failing)),
                threshold=0.0,
                affected=failing,
            )
        )
    infra_errors = [case.case_id for case in aggregate.cases if case.has_error]
    if infra_errors:
        rule_results.append(
            _rule(
                "run.infra_errors",
                "fail",
                observed=float(len(infra_errors)),
                threshold=0.0,
                affected=infra_errors,
                detail="ERROR 轮次：Gate 无法可靠评估（Spec §6.1 exit 2）",
            )
        )

    blocking_failures = [r for r in rule_results if r.verdict == "fail" and r.blocking]
    undetermined = [r for r in rule_results if r.verdict == "undetermined"]
    if blocking_failures:
        verdict = "fail"
    elif undetermined and all(r.verdict == "undetermined" for r in rule_results):
        verdict = "undetermined"
    else:
        verdict = "pass"

    notes: list[str] = []
    if aggregate.baseline_mode == BaselineMode.no_baseline.value:
        notes.append("NO_BASELINE：Gate 退化为绝对阈值模式（Spec §4.3）")
    if comparison is not None and not comparison.valid:
        notes.append(f"比较无效：{comparison.invalid_reason}")

    return GateReport(
        gate=rules.gate,
        run_id=aggregate.run.run_id,
        verdict=verdict,  # type: ignore[arg-type]
        baseline_mode=aggregate.baseline_mode,
        baseline_run_id=aggregate.run.baseline_run_id,
        rules=rule_results,
        aggregate=_junit_counts(aggregate),
        notes=notes,
    )


def _junit_counts(aggregate: RunAggregate) -> dict[str, int]:
    """§6.3：junit 的 failure/error 计数与 gate.json 必须同源，故在此一次算出。"""
    counts = {"cases": 0, "failures": 0, "errors": 0, "skipped": 0}
    for case in aggregate.cases:
        counts["cases"] += 1
        kind = case_status_for_junit(case)
        if kind == "failure":
            counts["failures"] += 1
        elif kind == "error":
            counts["errors"] += 1
        elif kind == "skipped":
            counts["skipped"] += 1
    return counts


def exit_code_for(gate: GateReport, aggregate: RunAggregate) -> int:
    """Spec §6.1: 0 PASS | 1 FAIL | 2 无法可靠评估 | 3 无效调用（由调用方给出）。

    ``suites.coverage`` 失败归 exit 2 而不是 1：Spec §6.1 把"基础设施错误导致
    mandatory suite 未完整执行"明确列在 exit 2 之下。归成 1 会让 CI 把它当成
    "PR 引入了回归"（§6.4 的归因约定），而真实原因是这次 run 根本没跑那些套件。
    """
    if any(case.has_error for case in aggregate.cases):
        return 2
    if any(r.verdict == "fail" and r.rule == "suites.coverage" for r in gate.rules):
        return 2
    if gate.verdict == "fail":
        return 1
    return 0
