"""统计置信（呈现层）。Spec §26：回答"这个差异有多少可能是噪声"。

边界（Spec §26.4 的裁决）：这些函数只进报告 / compare 统计块 / 校准规划器，
不进 Gate 求值、不改 RegressionState、不改 exit code——阈值仍是人的决定，
统计是让阈值"知情"的证据。全部纯 stdlib（NormalDist / math.comb / random），
与 stability.py 的 stdlib 先例一致，零新增依赖。
"""

from __future__ import annotations

import math
import random
import statistics
from statistics import NormalDist

# 默认显著性水平（Spec §26.4）：compare 统计块与校准规划共用一个口径。
DEFAULT_ALPHA = 0.05
_DEFAULT_CONFIDENCE = 0.95


def _z_score(confidence: float) -> float:
    """双侧置信水平对应的标准正态分位数（95% → 1.959964）。"""
    return NormalDist().inv_cdf(1 - (1 - confidence) / 2)


def wilson_ci(
    successes: int, total: int, confidence: float = _DEFAULT_CONFIDENCE
) -> tuple[float, float] | None:
    """比例的 Wilson score 区间。

    total == 0（无观测）返回 None：没有观测就没有区间——
    与"观测不到不进分母"的纪律同向（Spec §19.1.1），禁止把无观测坍缩成 [0, 1]。
    """
    if total <= 0:
        return None
    z_score = _z_score(confidence)
    observed = successes / total
    denominator = 1 + z_score * z_score / total
    center = (observed + z_score * z_score / (2 * total)) / denominator
    half = (
        z_score
        * math.sqrt(observed * (1 - observed) / total + z_score * z_score / (4 * total * total))
        / denominator
    )
    return (max(0.0, round(center - half, 6)), min(1.0, round(center + half, 6)))


def bootstrap_ci(
    values: list[float],
    confidence: float = _DEFAULT_CONFIDENCE,
    n_resamples: int = 2000,
    seed: int = 0,
) -> tuple[float, float] | None:
    """分数均值的 bootstrap 百分位区间（不对分布形状做假设）。

    同 seed 同输入 → 同区间（抽样确定性；报告可复现是本平台的事实纪律）。
    空输入返回 None（无观测，与 wilson_ci 同向）。

    注：这里是**非加密用途**——seed 是功能（可复现区间），换 secrets 反而失义。
    """
    if not values:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(len(values))] for _ in range(len(values))]
        means.append(statistics.mean(sample))
    means.sort()
    alpha = 1 - confidence
    lower = means[max(0, math.floor(alpha / 2 * len(means)) - 1)]
    upper = means[min(len(means) - 1, math.ceil((1 - alpha / 2) * len(means)) - 1)]
    return (round(lower, 6), round(upper, 6))


def mcnemar_exact_pvalue(regressions: int, improvements: int) -> float:
    """配对二值结局的 McNemar 精确检验（双侧）。

    两个参数是两个方向的翻转计数（Spec §26.4：baseline pass → candidate fail
    记 regressions，即经典记法的 b；反向记 improvements，即 c）。
    零假设是"两个方向等可能"。无翻转（b + c == 0）→ 完全一致，p = 1.0。
    """
    total_flips = regressions + improvements
    if total_flips == 0:
        return 1.0
    smaller_side = min(regressions, improvements)
    tail = sum(math.comb(total_flips, flip_count) for flip_count in range(0, smaller_side + 1)) / (
        2**total_flips
    )
    return round(min(1.0, 2 * tail), 6)


def two_proportion_ztest(
    successes_1: int, total_1: int, successes_2: int, total_2: int
) -> tuple[float | None, float | None]:
    """非配对双比例 z 检验（合并方差），返回 (z, p)。

    用于两侧样本**不是同一批 case** 的比例比较（在线评测窗口）。配对场景
    （同一 case 集合的两次 run）必须用 mcnemar_exact_pvalue——那是另一个检验。
    任一侧无观测返回 (None, None)。
    """
    if total_1 <= 0 or total_2 <= 0:
        return (None, None)
    rate_1 = successes_1 / total_1
    rate_2 = successes_2 / total_2
    if rate_1 == rate_2:
        return (0.0, 1.0)
    pooled_rate = (successes_1 + successes_2) / (total_1 + total_2)
    standard_error = math.sqrt(pooled_rate * (1 - pooled_rate) * (1 / total_1 + 1 / total_2))
    if standard_error == 0:
        # 合并方差为 0 只在 p̄ ∈ {0, 1} 时发生，而那时两侧比例必然相等——
        # 上一分支已返回。防御性兜底：无信号。
        return (0.0, 1.0)
    z_score = (rate_2 - rate_1) / standard_error
    p_value = 2 * (1 - NormalDist().cdf(abs(z_score)))
    return (round(z_score, 6), round(p_value, 6))


def minimum_detectable_delta(
    baseline_rate: float,
    group_size: int,
    alpha: float = DEFAULT_ALPHA,
    power: float = 0.8,
) -> float | None:
    """给定每组样本量 group_size，能以 (1-power) 风险检出的最小绝对差异（正态近似）。

    回答"20–50 cases 的 gate 到底能检出多大的差异"——阈值若小于该值，
    就是比噪声还窄的判据（Spec §26.4 的呈现层证据）。
    """
    if group_size <= 0 or not 0 <= baseline_rate <= 1 or not 0 < alpha < 1 or not 0 < power < 1:
        return None
    z_alpha = _z_score(1 - alpha)
    z_power = NormalDist().inv_cdf(power)
    delta = (z_alpha + z_power) * math.sqrt(2 * baseline_rate * (1 - baseline_rate) / group_size)
    return round(delta, 6)


def sample_size_for_precision(
    precision: float,
    rate: float = 0.5,
    confidence: float = _DEFAULT_CONFIDENCE,
) -> int | None:
    """比例 CI 半宽 ≤ precision 所需的样本量（rate=0.5 为最坏情形上界）。

    校准规划器（Spec §26.6）：标注是人力成本，先算"要 ±5pp 需要标多少条"
    再开标（95% / ±5pp / rate=0.5 → 385 条；±1pp → 9604 条）。
    """
    if precision <= 0 or not 0 <= rate <= 1 or not 0 < confidence < 1:
        return None
    z_score = _z_score(confidence)
    return math.ceil(z_score * z_score * rate * (1 - rate) / (precision * precision))
