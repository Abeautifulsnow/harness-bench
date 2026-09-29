"""报告产物生成（Spec V2.1.1 §6.2/§6.3；PRD §104 step 14）。

五个产物由同一个 ``RunAggregate`` 在一次写入中生成：report.json / gate.json /
junit.xml / report.html / summary.md。junit 的 failure+error 计数来自
gate.json 的 aggregate 块（§6.3 要求二者可反向核对），禁止分别推导。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from agent_eval.models.regression import GateReport
from agent_eval.models.results import Stability
from agent_eval.reports.aggregate import CaseAggregate, RunAggregate, case_status_for_junit

REPORT_SCHEMA = "agent-eval/report@v2"


def build_report(aggregate: RunAggregate) -> dict[str, Any]:
    """report.json：完整结果（CaseRun + MetricResult 聚合 + stability + regression）。"""
    run = aggregate.run
    payload: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "run_id": run.run_id,
        "benchmark_id": run.benchmark_id,
        "dataset": {
            "id": run.dataset_id,
            "version": run.dataset_version,
            "hash": run.dataset_hash,
        },
        "profile": run.profile,
        # PRD §108：本次覆盖的套件 → 选中 case 数（0 = 该套件没选出任何 case）
        "suites_covered": dict(run.suites_covered),
        "status": run.status.value,
        "verdict": aggregate.verdict,
        "baseline_mode": aggregate.baseline_mode,
        "baseline_run_id": run.baseline_run_id,
        "experiment_id": run.experiment_id,
        "variant_id": run.variant_id,
        "no_judge": run.no_judge,
        "metric_capability_snapshot": run.metric_capability_snapshot,
        "metric_degradations": run.metric_degradations,
        "warnings": aggregate.warnings,
        "totals": {
            "cases": aggregate.counts["cases"],
            "iterations": aggregate.counts["iterations"],
            "passed_iterations": aggregate.counts["passed_iterations"],
            "failed_iterations": aggregate.counts["failed_iterations"],
            "error_iterations": aggregate.counts["error_iterations"],
            "flaky_cases": aggregate.counts["flaky_cases"],
            "regression_cases": aggregate.counts["regression_cases"],
            "improved_cases": aggregate.counts["improved_cases"],
            "total_tokens": sum(
                int(case.tokens_mean * case.iterations) for case in aggregate.cases
            ),
            "total_cost": aggregate.cost.total_cost,
            "avg_tool_calls": _mean([c.tool_calls_mean for c in aggregate.cases]),
            "avg_latency_ms": _mean([c.latency_mean for c in aggregate.cases]),
        },
        "cost": aggregate.cost.as_dict(),
        "metrics": aggregate.metrics,
        "started_at": run.started_at.isoformat(),
        "finished_at": (run.finished_at or datetime.now().astimezone()).isoformat(),
        "cases": [_case_payload(case) for case in aggregate.cases],
    }
    if aggregate.comparison is not None:
        payload["regression"] = aggregate.comparison.model_dump(mode="json")
    return payload


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _case_payload(case: CaseAggregate) -> dict[str, Any]:
    # One read-model shape for both report.json and the REST API (CaseAggregate.as_dict):
    # a second hand-rolled mapping here is how the two silently drift apart.
    return case.as_dict()


# ---------------------------------------------------------------- junit / gate


def build_junit(aggregate: RunAggregate, gate: GateReport) -> str:
    """Spec §6.3：1 testcase = 1 Case（该 Case 全部 iteration 聚合）。

    failure/error 计数直接取自 gate.json 的 aggregate 块，二者同源。
    """
    suite = ET.Element(
        "testsuite",
        {
            "name": aggregate.run.benchmark_id,
            "tests": str(gate.aggregate.get("cases", len(aggregate.cases))),
            "failures": str(gate.aggregate.get("failures", 0)),
            "errors": str(gate.aggregate.get("errors", 0)),
            "skipped": str(gate.aggregate.get("skipped", 0)),
            "time": f"{sum(case.latency_mean for case in aggregate.cases) / 1000:.3f}",
            "timestamp": aggregate.run.started_at.isoformat(),
        },
    )
    properties = ET.SubElement(suite, "properties")
    for key, value in (
        ("run_id", aggregate.run.run_id),
        ("dataset_version", aggregate.run.dataset_version),
        ("profile", aggregate.run.profile),
        ("verdict", aggregate.verdict),
        ("baseline_mode", aggregate.baseline_mode),
    ):
        ET.SubElement(properties, "property", {"name": key, "value": str(value)})

    for case in aggregate.cases:
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": f"{aggregate.run.benchmark_id}.{_suite_of(case)}",
                "name": case.case_id,
                "time": f"{case.latency_mean / 1000:.3f}",
            },
        )
        kind = case_status_for_junit(case)
        if kind == "failure":
            node = ET.SubElement(
                testcase,
                "failure",
                {"message": f"blocking failures: {len(case.blocking_failures)}"},
            )
            node.text = _failure_text(case)
        elif kind == "error":
            node = ET.SubElement(
                testcase,
                "error",
                {"message": ",".join(case.error_semantics) or "evaluation/infra error"},
            )
            node.text = _failure_text(case)
        elif kind == "skipped":
            ET.SubElement(
                testcase,
                "skipped",
                {
                    "message": (
                        "no metric produced a verdict (all skipped: observation unavailable)"
                        if case.is_unjudged
                        else "no valid iteration"
                    )
                },
            )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")


def _suite_of(case: CaseAggregate) -> str:
    for tag in case.tags:
        if tag in {"smoke", "core", "regression", "security", "golden"}:
            return tag
    return "cases"


def _failure_text(case: CaseAggregate) -> str:
    lines = [
        f"iter{failure['iteration']} {failure['metric']} @ {failure.get('mount') or '-'}: "
        f"{failure['reason']}"
        for failure in case.blocking_failures
    ]
    lines += [f"semantics: {semantics}" for semantics in case.error_semantics]
    return "\n".join(lines)


def gate_payload(gate: GateReport, aggregate: RunAggregate) -> dict[str, Any]:
    payload = gate.model_dump(mode="json")
    payload["run_status"] = aggregate.run.status.value
    payload["warnings"] = aggregate.warnings
    return payload


# --------------------------------------------------------------------- writing


def write_reports(
    run_dir: Path,
    aggregate: RunAggregate,
    gate: GateReport,
    *,
    junit: str | None = None,
    html: str | None = None,
) -> dict[str, Path]:
    """一次写入五个产物（§6.2）。返回产物路径表，供 CLI/CI artifacts 使用。"""
    report = build_report(aggregate)
    paths = {
        "report.json": run_dir / "report.json",
        "gate.json": run_dir / "gate.json",
        "junit.xml": run_dir / "junit.xml",
        "report.html": run_dir / "report.html",
        "summary.md": run_dir / "summary.md",
    }
    _write_text(paths["report.json"], json.dumps(report, ensure_ascii=False, indent=2))
    _write_text(
        paths["gate.json"], json.dumps(gate_payload(gate, aggregate), ensure_ascii=False, indent=2)
    )
    _write_text(paths["junit.xml"], junit if junit is not None else build_junit(aggregate, gate))
    _write_text(paths["summary.md"], render_summary(report, gate))
    if html is not None:
        _write_text(paths["report.html"], html)
    return paths


def _write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def render_summary(report: dict[str, Any], gate: GateReport | None = None) -> str:
    """summary.md：PR 评论摘要（PRD §6.2）。"""
    lines: list[str] = []
    for warning in report.get("warnings", []):
        lines.append(f"> ⚠️ {warning}\n")
    if report["metric_degradations"]:
        lines.append("> Judge 降级（Spec §7.4）：")
        for metric_id, fallback in sorted(report["metric_degradations"].items()):
            lines.append(f"> - `{metric_id}` → `{fallback}`")
        lines.append("")
    totals = report["totals"]
    lines += [
        f"# Run {report['run_id']}",
        "",
        f"- Benchmark: `{report['benchmark_id']}` @ dataset "
        f"`{report['dataset']['id']}@{report['dataset']['version']}`",
        f"- Profile: `{report['profile']}` | Status: `{report['status']}` "
        f"| Verdict: **{report['verdict']}** | Baseline: `{report['baseline_mode']}`",
        f"- Cases: {totals['cases']} | Iterations: {totals['iterations']} "
        f"(PASS {totals['passed_iterations']} / FAIL {totals['failed_iterations']} "
        f"/ ERROR {totals['error_iterations']})",
        f"- Flaky: {totals['flaky_cases']} | Regression: {totals['regression_cases']} "
        f"| Improved: {totals['improved_cases']}",
        f"- Avg tool calls: {totals['avg_tool_calls']} | Avg latency: "
        f"{totals['avg_latency_ms']}ms | Tokens: {totals['total_tokens']} | Cost: "
        f"{totals['total_cost'] if totals['total_cost'] is not None else 'N/A（无定价表）'}",
        "",
    ]
    cost = report.get("cost") or {}
    if cost.get("unpriced_cases"):
        lines.append(
            f"> Cost 覆盖：{cost.get('priced_cases', 0)} / "
            f"{cost.get('priced_cases', 0) + cost.get('unpriced_cases', 0)} 个 CaseRun 有定价；"
            "其余未计入（PRD §59）\n"
        )
    if gate is not None:
        lines += ["## Gate", ""]
        lines.append(f"`{gate.gate}` → **{gate.verdict}**")
        lines.append("")
        lines.append("| Rule | Observed | Threshold | Verdict |")
        lines.append("|---|---|---|---|")
        for rule in gate.rules:
            observed = "-" if rule.observed is None else f"{rule.observed}"
            threshold = "-" if rule.threshold is None else f"{rule.threshold}"
            lines.append(f"| `{rule.rule}` | {observed} | {threshold} | {rule.verdict} |")
        lines.append("")

    comparison = report.get("regression")
    if comparison:
        validity = (
            "valid" if comparison["valid"] else f"INVALID: {comparison.get('invalid_reason')}"
        )
        lines += [
            "## Regression (PRD §105)",
            "",
            f"- Baseline `{comparison['baseline_run_id']}` → Candidate "
            f"`{comparison['candidate_run_id']}` ({validity})",
        ]
        counts = comparison.get("counts", {})
        lines.append(
            "- Cases: {cases} | Regression: {regression} | Improved: {improved} "
            "| Flaky: {flaky} | Unchanged: {unchanged} | Undetermined: {undetermined}".format(
                **{
                    k: counts.get(k, 0)
                    for k in (
                        "cases",
                        "regression",
                        "improved",
                        "flaky",
                        "unchanged",
                        "undetermined",
                    )
                }
            )
        )
        if comparison.get("performance_regressions"):
            lines.append(
                f"- PERFORMANCE_REGRESSION: {', '.join(comparison['performance_regressions'])}"
            )
        if comparison.get("failure_categories"):
            lines += ["", "| Failure Category | Baseline | Candidate | Δ |", "|---|---|---|---|"]
            for row in comparison["failure_categories"]:
                lines.append(
                    f"| `{row['category']}` | {row['baseline']} | {row['candidate']} "
                    f"| {row['delta']:+d} |"
                )
        lines.append("")

    lines += [
        "| Case | Stability | Pass Rate | Regression | Blocking Failures |",
        "|---|---|---|---|---|",
    ]
    for case in report["cases"]:
        lines.append(
            f"| `{case['case_id']}` | {case['stability']} | {case['pass_rate']:.0%} "
            f"| {case['regression_state']} | {len(case['blocking_failures'])} |"
        )
    failures = [
        (case, failure) for case in report["cases"] for failure in case["blocking_failures"]
    ]
    if failures:
        lines += ["", "## Blocking Failures", ""]
        for case, failure in failures:
            mount = failure.get("mount") or "-"
            lines.append(
                f"- `{case['case_id']}` iter{failure['iteration']} "
                f"{failure['metric']} @ {mount} ({failure['verdict']}): {failure['reason']}"
            )
    # case 级产物指针（PRD §90，ROADMAP 发现的 4）：CI 场景下只拿报告的人也要能
    # 找到失败现场。路径是 run 目录相对路径，summary.md 与 report.json 同目录落盘，
    # 相对链接因此成立。只列指针不塞内容（Spec §21.1）。
    pointers = [(case, item) for case in report["cases"] for item in case.get("artifacts", [])]
    if pointers:
        lines += ["", "## Case Artifacts (现场)", ""]
        for case, item in pointers:
            lines.append(
                f"- `{case['case_id']}` iter{item['iteration']} [{item['name']}]"
                f"({item['path']}) — {item['kind']}, {item['bytes']} bytes"
            )
    flaky = [c["case_id"] for c in report["cases"] if c["stability"] == Stability.FLAKY.value]
    if flaky:
        lines += ["", "## Flaky Cases (PRD §32)", ""]
        lines += [f"- `{case_id}`" for case_id in flaky]
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "REPORT_SCHEMA",
    "build_junit",
    "build_report",
    "case_status_for_junit",
    "gate_payload",
    "render_summary",
    "write_reports",
]
