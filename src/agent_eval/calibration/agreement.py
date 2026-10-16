"""一致性计算：judge 判定 vs 人工标注（Spec §26.2）。

正类 = judge FAIL。约定写死在快照的 ``positive_class`` 字段里——
precision / recall 的语义只有在正类声明在场时才可解读。
"""

from __future__ import annotations

from agent_eval.ids import new_id
from agent_eval.models.calibration import (
    CalibrationItem,
    CalibrationSet,
    CalibrationSnapshot,
    MetricAgreement,
)
from agent_eval.regression.confidence import wilson_ci


def _confusion(items: list[CalibrationItem]) -> tuple[int, int, int, int]:
    """返回 (tp, fp, tn, fn)，正类 = fail。"""
    tp = fp = tn = fn = 0
    for item in items:
        judge_fail = item.judge_verdict == "fail"
        human_fail = item.human_verdict == "fail"
        if judge_fail and human_fail:
            tp += 1
        elif judge_fail and not human_fail:
            fp += 1
        elif not judge_fail and not human_fail:
            tn += 1
        else:
            fn += 1
    return tp, fp, tn, fn


def compute_metric_agreement(metric: str, items: list[CalibrationItem]) -> MetricAgreement:
    """对"已标注"的 item 计算一致性；未标注项不进分母（分母 = labeled 数）。"""
    labeled = [i for i in items if i.human_verdict is not None]
    labeled_count = len(labeled)
    tp, fp, tn, fn = _confusion(labeled)
    agree = tp + tn
    near = [i for i in labeled if i.near_threshold]
    near_agree = sum(1 for i in near if i.judge_verdict == i.human_verdict)
    thresholds = {i.threshold for i in labeled}

    po = agree / labeled_count if labeled_count else 0.0
    p_judge_fail = (tp + fp) / labeled_count if labeled_count else 0.0
    p_human_fail = (tp + fn) / labeled_count if labeled_count else 0.0
    pe = p_judge_fail * p_human_fail + (1 - p_judge_fail) * (1 - p_human_fail)

    return MetricAgreement(
        metric=metric,
        n=labeled_count,
        agree=agree,
        accuracy=round(po, 6),
        accuracy_ci95=wilson_ci(agree, labeled_count),
        precision=round(tp / (tp + fp), 6) if (tp + fp) else None,
        recall=round(tp / (tp + fn), 6) if (tp + fn) else None,
        # pe == 1 意味着两侧边际完全退化（全是同一类），κ 无定义——如实为 None
        kappa=None if pe >= 1 else round((po - pe) / (1 - pe), 6),
        tp=tp,
        fp=fp,
        tn=tn,
        fn=fn,
        near_threshold_accuracy=round(near_agree / len(near), 6) if near else None,
        threshold=thresholds.pop() if len(thresholds) == 1 else None,
    )


def build_snapshot(cal_set: CalibrationSet) -> CalibrationSnapshot:
    """由当前标注状态构建快照（不落盘、不改 set——落盘是 store/CLI 的动作）。

    部分标注即可评估（Spec §26.6）：labeled / total 并排呈现，不假装全标。
    """
    labeled = [i for i in cal_set.items if i.human_verdict is not None]
    overall = compute_metric_agreement("overall", labeled)
    per_metric = [
        compute_metric_agreement(metric, [i for i in labeled if i.metric == metric])
        for metric in sorted({i.metric for i in labeled})
    ]
    return CalibrationSnapshot(
        snapshot_id=new_id("calsnap"),
        labeled=len(labeled),
        total=len(cal_set.items),
        judge_model=cal_set.judge_model,
        overall=overall,
        per_metric=per_metric,
    )
