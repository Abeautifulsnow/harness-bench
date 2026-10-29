"""从事实源抽取 judge 判定项并分层抽样（Spec §26.1）。

只校准语义 judge 指标（``agent.*`` 命名空间，Spec §7.1）：
verdict 必须是 pass / fail，score / threshold 必须可观测——skipped / error /
warning / 缺分量的判定**进不了样本**，但剔除计数随集合落盘（excluded），
"哪些判定没资格进样本"与样本本身一样是事实。
"""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict

from agent_eval.errors import InvalidCallError
from agent_eval.models.calibration import CalibrationItem
from agent_eval.models.results import CaseRunResult
from agent_eval.models.run import RunMetadata
from agent_eval.review.store import NEAR_THRESHOLD_MARGIN

_CALIBRATABLE_VERDICTS = {"pass", "fail"}


def _item_id(*parts: object) -> str:
    # 非加密用途：出处键的确定性指纹（幂等导入标注靠它），截断到 12 位即可
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]
    return f"cal_{digest}"


def collect_run_items(
    meta: RunMetadata, results: list[CaseRunResult]
) -> tuple[list[CalibrationItem], dict[str, int]]:
    """run 事实源：CaseRunResult 全挂载点上的 agent.* 判定（Spec §2.2 三挂载点同权）。"""
    items: list[CalibrationItem] = []
    excluded: dict[str, int] = defaultdict(int)
    for result in results:
        for metric in result.all_metric_results:
            if not metric.metric.startswith("agent."):
                continue
            if metric.verdict == "skipped":
                excluded["skipped"] += 1
                continue
            if metric.verdict == "error":
                excluded["error"] += 1
                continue
            if metric.verdict == "warning":
                excluded["warning"] += 1
                continue
            if metric.score is None or metric.threshold is None:
                excluded["unscored"] += 1
                continue
            items.append(
                CalibrationItem(
                    item_id=_item_id("run", meta.run_id, metric.id),
                    source="run",
                    source_id=meta.run_id,
                    case_id=result.case_id,
                    metric=metric.metric,
                    iteration=result.iteration,
                    score=metric.score,
                    threshold=metric.threshold,
                    judge_verdict=metric.verdict,  # type: ignore[arg-type]
                    near_threshold=abs(metric.score - metric.threshold) <= NEAR_THRESHOLD_MARGIN,
                )
            )
    return items, dict(excluded)


def collect_production_items(
    trace_id: str, evaluations: list[dict]
) -> tuple[list[CalibrationItem], dict[str, int]]:
    """production 事实源：trace 的全部 online eval rows（§61 的落盘形态）。"""
    items: list[CalibrationItem] = []
    excluded: dict[str, int] = defaultdict(int)
    for evaluation in evaluations:
        evaluation_id = str(evaluation.get("evaluation_id", ""))
        for index, row in enumerate(evaluation.get("rows", [])):
            verdict = str(row.get("verdict", ""))
            if verdict == "skipped":
                excluded["skipped"] += 1
                continue
            if verdict == "error":
                excluded["error"] += 1
                continue
            if verdict == "warning":
                # 生产 rows 现状产出不了 warning（_evaluate_one 只有四值）；与 run 源
                # 保持对称——真出现时计入剔除，而不是让 pydantic Literal 崩溃。
                excluded["warning"] += 1
                continue
            score = row.get("score")
            threshold = row.get("threshold")
            if score is None or threshold is None:
                excluded["unscored"] += 1
                continue
            items.append(
                CalibrationItem(
                    item_id=_item_id("prod", trace_id, evaluation_id, index),
                    source="production",
                    source_id=trace_id,
                    sub_id=evaluation_id,
                    case_id=str(row.get("case_id", "")),
                    metric=str(row.get("metric", "")),
                    score=float(score),
                    threshold=float(threshold),
                    judge_verdict=verdict,  # type: ignore[arg-type]
                    near_threshold=abs(float(score) - float(threshold)) <= NEAR_THRESHOLD_MARGIN,
                )
            )
    return items, dict(excluded)


def stratified_sample(
    items: list[CalibrationItem], target_n: int, seed: int
) -> list[CalibrationItem]:
    """按 metric 分层的确定性抽样：同 seed 同集合（可复现是台账的前提）。

    分层是校准的语义要求，不是统计装饰：抽样目标是"每个 judge 指标都能得到
    校准"，整体随机抽会被 metric 数量分布带偏（大 metric 吃掉全部配额）。
    配额用最大余数法分配到恰好 n；组内用 ``seed:metric`` 派生流洗牌，
    metric 配额不因其他 metric 的组内顺序而漂移。

    注：洗牌是**非加密用途**——seed 是功能（同 seed 同样本），不是秘密。
    """
    if target_n <= 0:
        raise InvalidCallError("--n 必须为正")
    if not items:
        raise InvalidCallError("没有可校准的 judge 判定（skipped/error/unscored 不进样本）")
    ordered = sorted(items, key=lambda item: (item.metric, item.case_id, item.item_id))
    if target_n >= len(ordered):
        return list(ordered)

    by_metric: dict[str, list[CalibrationItem]] = defaultdict(list)
    for item in ordered:
        by_metric[item.metric].append(item)
    total = len(ordered)
    quotas: dict[str, int] = {
        metric_name: target_n * len(group) // total for metric_name, group in by_metric.items()
    }
    remainder = target_n - sum(quotas.values())
    # 余数按小数部分从大到小补，平局按 metric 名——全流程无随机
    fractional = sorted(
        by_metric,
        key=lambda metric_name: (
            -(target_n * len(by_metric[metric_name]) / total - quotas[metric_name]),
            metric_name,
        ),
    )
    for metric_name in fractional[:remainder]:
        quotas[metric_name] += 1

    sampled: list[CalibrationItem] = []
    for metric, group in by_metric.items():
        rng = random.Random(f"{seed}:{metric}")
        picks = rng.sample(range(len(group)), min(quotas[metric], len(group)))
        sampled.extend(group[i] for i in picks)
    return sorted(sampled, key=lambda i: (i.metric, i.case_id, i.item_id))
