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

    # Capability snapshot for this run (Spec §7.3): metric id -> available
    metric_capability_snapshot: dict[str, bool] = Field(default_factory=dict)
    # metric id -> fallback metric id actually applied (Spec §7.4 degradation record)
    metric_degradations: dict[str, str] = Field(default_factory=dict)
    no_judge: bool = False

    cli_params: dict[str, Any] = Field(default_factory=dict)
