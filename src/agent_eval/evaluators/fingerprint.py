"""judge 配置指纹（Spec §26.8；补充 §26.5 的手工声明面）。

criteria_version（Spec §26.5）语义是"判准变更要递增"，靠 profile 作者自觉声明；
digest 从**生效**配置（判准版本 / judge_model / deepeval 版本 / 各 metric 阈值）
确定性算出，兜住"作者忘了递增 / 没声明"的盲区——threshold / 模型 / SDK 版本的
任何漂移从此在 compare 守卫里可见。两者并存（digest 相等 ⇒ criteria_version
相等；反之不成立），设计裁决见任务研究文档"自动指纹与手工声明的关系"。

本模块还提供 ``compute_eval_policy_digest``（Spec §26.9）：EvalPolicy
判分面指纹，与 judge 配置指纹同一算式家风——两个指纹函数共享
``_sorted_metric_entries`` 规范化助手，不复制算式。

纯 stdlib（hashlib + json）：指纹计算不允许引入 SDK 依赖。
"""

from __future__ import annotations

import hashlib
import json

from agent_eval.models.profile import MetricSpec


def _sorted_metric_entries(entries: list[tuple[str, float | None]]) -> list[dict]:
    """metric (id, threshold) 列表 → 规范化条目，按规范化 JSON 全序排序。

    两个指纹函数共享同一排序与条目形状——"同算式家风"是 digest 可比性的前提，
    不是可各自复制的实现细节。排序键与条目本身同构，声明顺序不影响 digest。
    """
    metrics = [{"metric": metric_id, "threshold": threshold} for metric_id, threshold in entries]
    metrics.sort(key=lambda entry: json.dumps(entry, sort_keys=True))
    return metrics


def compute_judge_config_digest(
    *,
    criteria_version: str | None,
    judge_model: str | None,
    deepeval_version: str | None,
    judge_specs: list[MetricSpec],
) -> str | None:
    """生效 judge 配置 → sha256 前 16 位 hex；无生效 judge 口径 → None。

    ``judge_specs`` 必须是解析与 degradation 之后的生效集。为空（--no-judge 或
    probe 全 False）返回 None——"没有生效的 judge 口径"如实缺席，不造空串指纹
    （None 与 0/空串是两种事实，质量规范"数值语义"条）。

    确定性是"同口径"判定的前提：metrics 列表按条目的规范化 JSON 排序，
    profile 内 metric 声明顺序不影响 digest；同一配置必然同 digest。

    边界：``judge_model`` 为 None 时 SDK 用环境决定的默认模型——指纹只能覆盖
    声明面，该盲区如实存在（Spec §26.8.1）。
    """
    if not judge_specs:
        return None
    metrics = _sorted_metric_entries([(spec.id, spec.threshold) for spec in judge_specs])
    payload = {
        "criteria_version": criteria_version,
        "judge_model": judge_model,
        "deepeval_version": deepeval_version,
        "metrics": metrics,
    }
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_eval_policy_digest(
    *,
    judge_model: str | None,
    policy_metrics: list[tuple[str, float | None]],
) -> str:
    """EvalPolicy 判分面 → sha256 前 16 位 hex（Spec §26.9）。

    只覆盖判分面：judge_model + metrics 的 (id, threshold)。policy 的 id /
    display_name / description / timeout_seconds **不进**摘要——改名、改描述、
    改超时不改变判法（timeout 决定"能不能出分"，不决定"怎么打分"）。
    policy 恒有 metrics 定义面，无"无判分面"分支：metrics 为空的 policy 照算
    digest，空表语义由既有执行路径处理，不在此扩大。

    参数取标量字段而非 EvalPolicy 对象：fingerprint 只依赖 models 层，
    不与 evaluators 内部互相导入（调用方自带 policy → 标量的提取点在
    online_eval.policy_digest）。
    """
    payload = {
        "judge_model": judge_model,
        "metrics": _sorted_metric_entries(policy_metrics),
    }
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
