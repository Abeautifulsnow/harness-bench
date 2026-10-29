"""Judge Calibration 数据模型（Spec §26）。

校准的对象是"judge 已做出的判定"：每个 item 是一条 score / threshold / verdict
齐备的判定事实 + 人工对**同一条判定**的标注。正类 = judge FAIL（Spec §26.2）：
校准回答的两个问题是"judge 判死的是否冤枉（precision）"与"该死的有没有漏判（recall）"。

与 Review Store 的边界（Spec §26.1）：review 是 case 级五值人工结论，queue_reason
词汇封闭（PRD §61 六值不得增删）；校准是 per-metric 二值标注，走独立的
``state/calibrations/`` 存储，不复用、不扩 Review 的词汇。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 人工标注的取值：与 judge 的 pass/fail 同词汇表——一致性检验要求两侧可比
HUMAN_VERDICTS = ("pass", "fail")

# 分歧归因三分法（Spec §26.8.5，词汇封闭；出处与分析见该节导语）：
# input_missing = judge 该看到的东西不在输入里 / rubric_ambiguous = 标准边界模糊 /
# model_misunderstanding = 标准清晰但模型读偏。归因只能写在分歧 item 上（CLI/import
# 侧校验）；调整词汇须同步 Literal 与 spec 回写。
DISAGREEMENT_CAUSES = ("input_missing", "rubric_ambiguous", "model_misunderstanding")


class CalibrationItem(BaseModel):
    """一条等待（或已被）人工标注的 judge 判定。

    ``item_id`` 由出处键哈希而来：同一条判定重复抽取得到同一个 id，
    幂等导入标注依赖这一点。
    """

    item_id: str
    source: Literal["run", "production"]
    source_id: str  # run_id / trace_id
    sub_id: str = ""  # production: evaluation_id；run 源为空
    case_id: str
    metric: str
    iteration: int | None = None
    score: float
    threshold: float
    judge_verdict: Literal["pass", "fail"]
    near_threshold: bool = False  # |score - threshold| <= NEAR_THRESHOLD_MARGIN
    human_verdict: Literal["pass", "fail"] | None = None
    # 双盲第二标（round 2）：与人标一互不覆盖——人人一致性天花板（Spec §26.8）
    # 需要两个独立标注人，单标永远算不出上限参照。
    human_verdict_2: Literal["pass", "fail"] | None = None
    human_rater: str = ""  # 标注人 provenance（round 1）
    human_rater_2: str = ""  # 标注人 provenance（round 2）
    # 不一致归因：仅当 human_verdict 与 judge_verdict 分歧时才允许写（CLI/import
    # 校验），词汇取上面的 DISAGREEMENT_CAUSES。
    disagreement_cause: (
        Literal["input_missing", "rubric_ambiguous", "model_misunderstanding"] | None
    ) = None
    note: str = ""


class MetricAgreement(BaseModel):
    """单个 metric（或 overall）的一致性结论——校准快照的一行。

    混淆矩阵以 fail 为正类：tp = 判死且人工同意，fp = 判死但人工说 pass（冤枉），
    tn = 判过且人工同意，fn = 判过但人工说 fail（漏判）。
    """

    metric: str
    n: int = 0
    agree: int = 0
    accuracy: float = 0.0
    accuracy_ci95: tuple[float, float] | None = None
    precision: float | None = None  # P(human FAIL | judge FAIL)
    recall: float | None = None  # P(judge FAIL | human FAIL)
    kappa: float | None = None  # Cohen's κ；边际退化（pe=1）时为 None
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    near_threshold_accuracy: float | None = None
    threshold: float | None = None  # 该 metric 的生效阈值（回答"阈值从哪来"）
    # 人人一致性天花板参照（Spec §26.8）：rater1 vs rater2 的 Cohen's κ，
    # 只在双标齐备的 item 上算；边际退化（pe ≥ 1）→ None，与 judge κ 同规则。
    # judge κ 显著低于人人 κ 才是 judge 的问题；人人 κ 本身低说明 rubric 边界模糊。
    human_kappa: float | None = None
    human_dual_n: int = 0  # 参与天花板估计的 item 数
    # 分歧归因账目：total = labeled 且 human_verdict ≠ judge_verdict；
    # causes 只含有 cause 的条目，未归因数 = total − sum(causes)，不单独造字段。
    disagreement_total: int = 0
    disagreement_causes: dict[str, int] = Field(default_factory=dict)


class CalibrationSnapshot(BaseModel):
    """一次 ``calibrate evaluate`` 的结论，追加进 set（不覆盖历史快照）。"""

    snapshot_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    labeled: int
    total: int
    positive_class: Literal["fail"] = "fail"
    judge_model: str | None = None
    # 判准版本与 judge 配置指纹（Spec §26.5）：run 源从 RunMetadata 落账，
    # production 源为 None（不猜）。快照回答"这份一致性结论描述的是哪个判准版本"。
    criteria_version: str | None = None
    judge_config_digest: str | None = None
    overall: MetricAgreement
    per_metric: list[MetricAgreement] = Field(default_factory=list)


class CalibrationSet(BaseModel):
    """一次抽样的全部事实：items + 标注 + 快照历史，一文件一事（Spec §26.6）。"""

    cal_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    source_kind: Literal["run", "production"]
    source_ids: list[str]
    seed: int
    requested_n: int
    judge_model: str | None = None
    # run 源的 set 天然单一口径：从 RunMetadata 落账；production 源两字段 None。
    # 缺省 None = 旧 set 文件（无新字段）照常加载。
    criteria_version: str | None = None
    judge_config_digest: str | None = None
    items: list[CalibrationItem] = Field(default_factory=list)
    # 抽样期剔除的 judge 判定计数（skipped / error / warning / unscored）。
    # "哪些判定进不了样本"必须与样本本身一样可见——否则覆盖率会虚高。
    excluded: dict[str, int] = Field(default_factory=dict)
    snapshots: list[CalibrationSnapshot] = Field(default_factory=list)
