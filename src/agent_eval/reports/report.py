"""report.json + summary.md 生成（Spec V2.1.1 §6.2 的 P0 子集）。"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_eval.models.results import CaseRunResult, CaseStability, CaseStatus, RegressionState
from agent_eval.models.run import RunMetadata, RunStatus


def compute_verdict(meta: RunMetadata, results: list[CaseRunResult]) -> str:
    """Spec §6.1 归类：partial+infra → infra_failure；有 blocking FAIL → fail；否则 pass。"""
    if meta.status == RunStatus.partial:
        return "infra_failure"
    if any(r.blocking_failed for r in results):
        return "fail"
    return "pass"


def build_report(
    meta: RunMetadata,
    results: list[CaseRunResult],
    stabilities: list[CaseStability],
) -> dict[str, Any]:
    stability_by_case = {s.case_id: s for s in stabilities}
    case_ids = sorted({r.case_id for r in results})
    cases: list[dict[str, Any]] = []
    totals = Counter()
    tokens_total = 0
    for case_id in case_ids:
        iterations = sorted([r for r in results if r.case_id == case_id], key=lambda r: r.iteration)
        stability = stability_by_case.get(case_id)
        blocking_failures = [
            {
                "iteration": r.iteration,
                "metric": m.metric,
                "mount": m.metadata.get("mount"),
                "turn": m.metadata.get("turn"),
                "reason": m.reason,
                "verdict": m.verdict,
            }
            for r in iterations
            for m in r.all_metric_results  # 含 turn 级挂载点（Spec §2.2）
            if m.blocking and m.verdict in {"fail", "error"}
        ]
        for r in iterations:
            totals[f"status_{r.status.value}"] += 1
            tokens_total += r.token_count
        cases.append(
            {
                "case_id": case_id,
                "case_version": iterations[0].case_version,
                "iterations": len(iterations),
                "stability": stability.stability.value if stability else "UNKNOWN",
                "pass_rate": stability.pass_rate if stability else 0.0,
                "regression_state": (
                    stability.regression_state.value
                    if stability
                    else RegressionState.UNDETERMINED.value
                ),
                "infra_error_count": stability.infra_error_count if stability else 0,
                "blocking_failures": blocking_failures,
            }
        )
    return {
        "schema": "agent-eval/report@p0",
        "run_id": meta.run_id,
        "benchmark_id": meta.benchmark_id,
        "dataset": {
            "id": meta.dataset_id,
            "version": meta.dataset_version,
            "hash": meta.dataset_hash,
        },
        "profile": meta.profile,
        "status": meta.status.value,
        "verdict": compute_verdict(meta, results),
        "baseline_mode": meta.baseline_mode,
        "no_judge": meta.no_judge,
        "metric_capability_snapshot": meta.metric_capability_snapshot,
        "metric_degradations": meta.metric_degradations,
        "totals": {
            "cases": len(cases),
            "iterations": sum(
                totals[f"status_{s.value}"]
                for s in (CaseStatus.PASS, CaseStatus.FAIL, CaseStatus.ERROR)
            ),
            "passed_iterations": totals["status_PASS"],
            "failed_iterations": totals["status_FAIL"],
            "error_iterations": totals["status_ERROR"],
            "total_tokens": tokens_total,
            # P0 不计算成本（PRD §59 属 P2）：上报 None 而不是伪造一个 0.0
            "total_cost": None,
        },
        "started_at": meta.started_at.isoformat(),
        "finished_at": (meta.finished_at or datetime.now().astimezone()).isoformat(),
        "cases": cases,
    }


def render_summary(report: dict[str, Any]) -> str:
    lines: list[str] = []
    if report["baseline_mode"] == "NO_BASELINE":
        lines.append(
            "> **NO BASELINE**：无合格历史 run，回归判定不可用（Spec V2.1.1 §4.3，不阻塞 Gate）\n"
        )
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
        f"| Verdict: **{report['verdict']}**",
        f"- Cases: {totals['cases']} | Iterations: {totals['iterations']} "
        f"(PASS {totals['passed_iterations']} / FAIL {totals['failed_iterations']} "
        f"/ ERROR {totals['error_iterations']})",
        f"- Tokens: {totals['total_tokens']} | Cost: "
        f"{totals['total_cost'] if totals['total_cost'] is not None else 'N/A (P0 不计成本)'}",
        "",
        "| Case | Stability | Pass Rate | Regression | Blocking Failures |",
        "|---|---|---|---|---|",
    ]
    for case in report["cases"]:
        n_fail = len(case["blocking_failures"])
        lines.append(
            f"| `{case['case_id']}` | {case['stability']} | {case['pass_rate']:.0%} "
            f"| {case['regression_state']} | {n_fail} |"
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
    lines.append("")
    return "\n".join(lines)


def write_report(run_dir: Path, report: dict[str, Any], summary_md: str) -> None:
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(summary_md, encoding="utf-8")
