"""Run-level models: metadata (PRD §30), status (§29), baseline policy (Spec §4)."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    partial = "partial"  # some iterations hit infra/evaluation failure (Spec §6.1)
    failed = "failed"
    cancelled = "cancelled"


class FailureSemantics(StrEnum):
    """PRD §46: judge failure != agent failure."""

    AGENT = "AGENT_FAILURE"
    EVALUATION = "EVALUATION_FAILURE"
    INFRA = "INFRA_FAILURE"


class RunMetadata(BaseModel):
    """Everything needed to reproduce a run (PRD §30/§109.3 + Spec §7.3/§4.5)."""

    run_id: str
    benchmark_id: str
    dataset_id: str
    dataset_version: str
    dataset_hash: str
    profile: str
    suite: str | None = None
    tag_filter: list[str] = Field(default_factory=list)
    # PRD §108：本次 run 实际覆盖的套件 → 最终选中的 case 数。
    # 是"必跑套件"校验的唯一事实源：被 tag 过滤掉、或套件本身选不出 case 的
    # 套件在这里是 0，而不是"跑过了"。缺省空字典 = 旧 run，按未覆盖处理。
    suites_covered: dict[str, int] = Field(default_factory=dict)
    # PRD §22–§25: an experiment run belongs to exactly one variant (Spec §1.2)
    experiment_id: str | None = None
    variant_id: str | None = None
    variant: str | None = None
    agent_version: str | None = None
    agent_endpoint: str = ""
    git_commit: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    agent_model: str | None = None
    judge_model: str | None = None
    deepeval_version: str | None = None
    eval_platform_version: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    finished_at: datetime | None = None
    environment: str = "local"  # PRD §88: local | docker | remote
    status: RunStatus = RunStatus.queued

    # Baseline policy (Spec §4.5): recorded per run; NO_BASELINE when unresolved.
    baseline_policy: str | None = None
    baseline_run_id: str | None = None
    baseline_mode: str = "NO_BASELINE"
    baseline_reason: str | None = None  # why the baseline is unresolved (§4.3)

    # Capability snapshot for this run (Spec §7.3)。**两类键共存于此字段**，注释即契约：
    #   - 裸 metric id → bool：DeepEval probe() 的能力探测（judge 缺失与否）；
    #   - `event:<PRD §8 事件名>` → bool：外部 SUT 的观测面能力表（E2/A2，health 阶段上报）。
    # 报告上"两个 False"必须能分清是 judge 缺失还是观测面缺失——前缀就是区分手段。
    metric_capability_snapshot: dict[str, bool] = Field(default_factory=dict)
    # metric id -> fallback metric id actually applied (Spec §7.4 degradation record)
    metric_degradations: dict[str, str] = Field(default_factory=dict)
    no_judge: bool = False

    # E1（PRD §8 闭合词汇表）：执行期观测到的未知事件类型 → 次数。计数在执行期累加、
    # 聚合期并入 aggregate.warnings（默认 warn，可见不阻断；显式开关升级为 exit 2）。
    protocol_violations: dict[str, int] = Field(default_factory=dict)
    # A3 修订五：token 用量口径——本 run 的用量观测到了哪些分量。
    #   full = 输入输出双侧都观测到；partial = 存在单侧观测的 case（总量被低估——
    #   外部流上只报输入侧用量的 SUT 都是这种形态）；None = 全程没有任何用量观测。
    # 基线比对时两侧口径不一致 → INVALID（compare.py 守卫，Spec §4.3 禁止不可比比较）。
    token_usage_scope: str | None = None
    # run 级可见性通道（如 A1 workdir 回执缺失）；聚合期并入 aggregate.warnings。
    warnings: list[str] = Field(default_factory=list)

    cli_params: dict[str, Any] = Field(default_factory=dict)
