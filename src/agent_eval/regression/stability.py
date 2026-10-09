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


def _pass_at_k(valid: list[CaseRunResult], repeat: int) -> dict[str, float]:
    """PRD §31：pass@k 仅当 repeat ≥ k 时输出，否则该 key 不出现。

    V1 口径：repeat 次里至少一次 PASS 即 pass@k=1.0（k 次独立试验的成功覆盖）。
    """
    passes = sum(1 for it in valid if it.status == CaseStatus.PASS)
    out: dict[str, float] = {}
    for k in (1, 3, 5):
        if repeat >= k:
            out[f"pass@{k}"] = 1.0 if passes > 0 else 0.0
    return out


def _pass_hat_k(valid: list[CaseRunResult], repeat: int) -> dict[str, float]:
    """pass^k：k 次必须全部成功（安全/权限/敏感操作的保守口径）。

    与 pass@k 并列输出、互为对照：exploration 能力看 pass@k，"从不失败"看 pass^k。
    ERROR 轮已在上游剔除——它不是 FAIL 也不是 PASS，不参与"全部成功"判定，
    否则基础设施抖动会把稳定 case 的 pass^k 静默打成 0。
    """
    passes = sum(1 for it in valid if it.status == CaseStatus.PASS)
    out: dict[str, float] = {}
    for k in (1, 3, 5):
        if repeat >= k:
            out[f"pass^{k}"] = 1.0 if len(valid) > 0 and passes == len(valid) else 0.0
    return out


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
    tokens = [float(it.token_count) for it in valid]
    repeat = len(iterations)
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
        token_variance=(
            round(statistics.stdev(tokens) / statistics.mean(tokens), 6)
            if len(tokens) >= 2 and statistics.mean(tokens) > 0
            else None
        ),
        pass_at_k=_pass_at_k(valid, repeat),
        pass_hat_k=_pass_hat_k(valid, repeat),
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
