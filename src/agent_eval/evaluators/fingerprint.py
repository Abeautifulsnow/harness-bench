"""judge 配置指纹（Spec §26.8；补充 §26.5 的手工声明面）。

criteria_version（Spec §26.5）语义是"判准变更要递增"，靠 profile 作者自觉声明；
digest 从**生效**配置（判准版本 / judge_model / deepeval 版本 / 各 metric 阈值）
确定性算出，兜住"作者忘了递增 / 没声明"的盲区——threshold / 模型 / SDK 版本的
任何漂移从此在 compare 守卫里可见。两者并存（digest 相等 ⇒ criteria_version
相等；反之不成立），设计裁决见任务研究文档"自动指纹与手工声明的关系"。

纯 stdlib（hashlib + json）：指纹计算不允许引入 SDK 依赖。
"""

from __future__ import annotations

import hashlib
import json

from agent_eval.models.profile import MetricSpec


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
    metrics = [{"metric": spec.id, "threshold": spec.threshold} for spec in judge_specs]
    metrics.sort(key=lambda entry: json.dumps(entry, sort_keys=True))
    payload = {
        "criteria_version": criteria_version,
        "judge_model": judge_model,
        "deepeval_version": deepeval_version,
        "metrics": metrics,
    }
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
