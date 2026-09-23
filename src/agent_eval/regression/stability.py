"""Stability 判定算法（Spec V2.1.1 §3，V1 规则禁止隐式扩展）。

ERROR（EVALUATION/INFRA）轮剔除，不参与统计，单独记 infra_error_count。
"""

from __future__ import annotations

import statistics

from agent_eval.models.results import CaseRunResult, CaseStability, CaseStatus, Stability


def _tool_sequence_variance(iterations: list[CaseRunResult]) -> float | None:
    """各 iteration 工具序列差异度：成对 Jaccard 距离均值（0=完全一致）。"""
    seqs = [tuple(t.name for t in it.tool_calls) for it in iterations]
    if len(seqs) < 2:
        return None
    distances: list[float] = []
    for i in range(len(seqs)):
        for j in range(i + 1, len(seqs)):
            a, b = set(seqs[i]), set(seqs[j])
            union = a | b
            distances.append(0.0 if not union else 1.0 - len(a & b) / len(union))
    return round(statistics.mean(distances), 6)


def _latency_cv(iterations: list[CaseRunResult]) -> float | None:
    latencies = [float(it.latency_ms) for it in iterations]
    if len(latencies) < 2 or statistics.mean(latencies) == 0:
        return None
    return round(statistics.stdev(latencies) / statistics.mean(latencies), 6)


def compute_stability(case_id: str, iterations: list[CaseRunResult]) -> CaseStability:
    valid = [it for it in iterations if it.status in {CaseStatus.PASS, CaseStatus.FAIL}]
    infra_errors = [it for it in iterations if it.status == CaseStatus.ERROR]
    passes = sum(1 for it in valid if it.status == CaseStatus.PASS)
    pass_rate = (passes / len(valid)) if valid else 0.0

    if len(valid) >= 3:
        if passes == len(valid):
            stability = Stability.STABLE_PASS
        elif passes == 0:
            stability = Stability.STABLE_FAIL
        else:
            stability = Stability.FLAKY
    else:
        stability = Stability.UNKNOWN

    scores = [
        m.score
        for it in valid
        for m in it.metric_results
        if m.score is not None and m.metric.startswith("agent.")
    ]
    return CaseStability(
        case_id=case_id,
        stability=stability,
        pass_rate=round(pass_rate, 6),
        valid_iterations=len(valid),
        infra_error_count=len(infra_errors),
        score_mean=round(statistics.mean(scores), 6) if scores else None,
        score_stddev=round(statistics.stdev(scores), 6) if len(scores) >= 2 else None,
        tool_sequence_variance=_tool_sequence_variance(valid),
        latency_cv=_latency_cv(valid),
    )


def regression_state(
    baseline: Stability | None,
    candidate: Stability,
    candidate_invalid: bool = False,
) -> str:
    """Spec §3.3 判定表（V1）。P0 无 baseline → UNDETERMINED；数据损坏 → INVALID。"""
    if candidate_invalid:
        return "INVALID"
    if baseline is None:
        return "UNDETERMINED"
    table = {
        (Stability.STABLE_PASS, Stability.STABLE_PASS): "UNCHANGED",
        (Stability.STABLE_PASS, Stability.STABLE_FAIL): "REGRESSION",
        (Stability.STABLE_PASS, Stability.FLAKY): "REGRESSION",
        (Stability.STABLE_FAIL, Stability.STABLE_PASS): "IMPROVED",
        (Stability.STABLE_FAIL, Stability.STABLE_FAIL): "UNCHANGED",
        (Stability.STABLE_FAIL, Stability.FLAKY): "UNCHANGED",
    }
    if baseline == Stability.FLAKY:
        return "FLAKY"
    if baseline == Stability.UNKNOWN or candidate == Stability.UNKNOWN:
        return "UNDETERMINED"
    return table.get((baseline, candidate), "UNDETERMINED")
