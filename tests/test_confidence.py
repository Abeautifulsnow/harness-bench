"""统计置信模块的闭式解对照测试（Spec §26，公式锚点见任务 research 笔记 §4）。"""

from __future__ import annotations

import math

from agent_eval.regression.confidence import (
    bootstrap_ci,
    mcnemar_exact_pvalue,
    minimum_detectable_delta,
    sample_size_for_precision,
    two_proportion_ztest,
    wilson_ci,
)


class TestWilsonCi:
    def test_known_value_matches_closed_form(self) -> None:
        # 8/10 @95% 的 Wilson 区间与 scipy/statsmodels proportion_confint 一致
        lo, hi = wilson_ci(8, 10)
        assert lo == 0.490162
        assert hi == 0.943318

    def test_half_width_is_narrower_than_wald_for_small_n(self) -> None:
        # n=20、p=0.9 时 Wald 区间会越界到 1.02，Wilson 必须收在 [0,1] 内
        lo, hi = wilson_ci(18, 20)
        assert 0 <= lo <= hi <= 1

    def test_no_observation_returns_none(self) -> None:
        # 无观测 → 无区间；禁止把"没跑过"坍缩成 [0, 1]
        assert wilson_ci(0, 0) is None

    def test_confidence_widens_interval(self) -> None:
        narrow = wilson_ci(8, 10, confidence=0.90)
        wide = wilson_ci(8, 10, confidence=0.99)
        assert narrow is not None and wide is not None
        assert wide[0] < narrow[0] and wide[1] > narrow[1]


class TestBootstrapCi:
    def test_deterministic_for_same_seed(self) -> None:
        values = [0.8, 0.9, 0.7, 1.0, 0.6]
        assert bootstrap_ci(values, seed=42) == bootstrap_ci(values, seed=42)

    def test_interval_brackets_the_mean(self) -> None:
        values = [0.8, 0.9, 0.7, 1.0, 0.6]
        lo, hi = bootstrap_ci(values, seed=7)
        assert lo <= sum(values) / len(values) <= hi

    def test_empty_input_returns_none(self) -> None:
        assert bootstrap_ci([]) is None


class TestMcNemar:
    def test_known_value(self) -> None:
        # b=1, c=9：P(X<=1|n=10,0.5)=11/1024，双侧 ≈ 0.0215
        assert mcnemar_exact_pvalue(1, 9) == 0.021484

    def test_no_discordant_pairs_is_fully_concordant(self) -> None:
        assert mcnemar_exact_pvalue(0, 0) == 1.0

    def test_symmetric_in_b_and_c(self) -> None:
        assert mcnemar_exact_pvalue(2, 7) == mcnemar_exact_pvalue(7, 2)

    def test_extreme_imbalance_is_significant(self) -> None:
        assert mcnemar_exact_pvalue(0, 10) < 0.01

    def test_never_exceeds_one(self) -> None:
        # 小翻转计数时 2*tail 可能名义上 ≥1，必须截断
        assert mcnemar_exact_pvalue(3, 3) == 1.0


class TestTwoProportionZTest:
    def test_no_observation_returns_none(self) -> None:
        assert two_proportion_ztest(5, 0, 3, 10) == (None, None)

    def test_equal_proportions_no_signal(self) -> None:
        z, p = two_proportion_ztest(5, 10, 10, 20)
        assert z == 0.0
        assert p == 1.0

    def test_known_direction_and_significance(self) -> None:
        # baseline 40% → candidate 70%，n=50 每组：z ≈ +3.015（p2-p1 方向），显著
        z, p = two_proportion_ztest(20, 50, 35, 50)
        assert z is not None and p is not None
        assert z > 0
        assert p < 0.01

    def test_symmetric_in_groups(self) -> None:
        z1, _ = two_proportion_ztest(20, 50, 35, 50)
        z2, _ = two_proportion_ztest(35, 50, 20, 50)
        assert z1 is not None and z2 is not None
        assert math.isclose(z1, -z2)


class TestPlanning:
    def test_sample_size_known_values(self) -> None:
        # p=0.5 最坏情形：±5pp → 385；±1pp → 9604（标准公式 n = z²·p(1-p)/e² 向上取整）
        assert sample_size_for_precision(0.05) == 385
        assert sample_size_for_precision(0.01) == 9604

    def test_extreme_p_needs_fewer_samples(self) -> None:
        assert sample_size_for_precision(0.05, rate=0.9) < sample_size_for_precision(0.05, rate=0.5)

    def test_invalid_precision_returns_none(self) -> None:
        assert sample_size_for_precision(0.0) is None

    def test_mde_shrinks_with_n(self) -> None:
        small = minimum_detectable_delta(0.9, 20)
        large = minimum_detectable_delta(0.9, 200)
        assert small is not None and large is not None
        assert large < small

    def test_mde_quantifies_gate_blindness(self) -> None:
        # PR gate 的现实规模：n=50、基线 90% 时，能可靠检出的差异约 ±12pp——
        # 1% 的 max_regression_percent 阈值在统计上不可辨识（Spec §26.4 的证据）
        mde = minimum_detectable_delta(0.9, 50)
        assert mde is not None
        assert mde > 0.05
