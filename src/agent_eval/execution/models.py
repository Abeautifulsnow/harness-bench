"""Execution Plane 域模型（设计文档 §15/§33/§46）。

Job ≠ Run（§33）：Job 是"用户请求执行一次评测"的生命周期载体，Run 是 Runner
实际产生的一次评测。Job 记录放在 ``<data_root>/jobs/``，绝不写进 ``runs/``；
Run 的可复现事实（git commit、dataset version、model、judge model）继续由
run.json 承载（§37 的审计通过 job.run_id 关联，不复制）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now().astimezone()


class JobStatus(StrEnum):
    """Job 状态机（§15）。QUEUED → RUNNING → SUCCEEDED/FAILED；取消路径见 §17。"""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class FailureKind(StrEnum):
    """Job 级失败语义（§32 的 Job 侧子集；case 级失败语义仍在 run 报告里）。

    Run 正常产出（哪怕 verdict=fail / 有 ERROR case）也算 Job SUCCEEDED——
    "被测方失败了"是评测结论，不是调度事故，二者不能显示成同一种红色。
    """

    INFRA_FAILURE = "INFRA_FAILURE"  # agent 连不上 / 评测基础设施故障（exit 2 语义）
    INVALID_REQUEST = "INVALID_REQUEST"  # 定义树里没有这个东西（exit 3 语义）
    INTERNAL = "INTERNAL"  # 意料之外的崩溃，原样留痕


class EvalRunRequest(BaseModel):
    """统一的内部请求模型（§46）。CLI 与 Web 共享同一套语义，不各说各话。

    ``judge_concurrency`` 刻意不在模型里：judge 并发由 Git 中的 profile 声明，
    运行时覆盖它违反 §12 的 Profile 原则。
    """

    agent_profile: str
    benchmark: str
    suite: list[str] | None = None
    profile: str | None = None

    repeat: int | None = None

    # 映射到 RunConfig.concurrency（agent 侧并发，CLI --concurrency）；None = Runner 缺省。
    agent_concurrency: int | None = None

    no_judge: bool = False
    strict_protocol: bool = False
    save_trace: bool = True

    tags: list[str] = Field(default_factory=list)

    baseline_policy: str | None = None
    baseline_run: str | None = None


class JobProgress(BaseModel):
    """Run-level progress（§18：V1 只推 run 级，不逐事件推 trace）。

    ``total_trials`` 由 Runner 在选定 case 后回填；在此之前是 None（队列里
    还不知道会跑多少），UI 显示"排队中"而不是编一个 0。
    """

    total_trials: int | None = None
    completed_trials: int = 0
    passed: int = 0
    failed: int = 0
    error: int = 0

    def merged(self, counts: dict[str, int]) -> JobProgress:
        return self.model_copy(update={k: v for k, v in counts.items() if v is not None})


class EvalRunJob(BaseModel):
    """一次评测请求的完整生命周期记录（§22 必须保存的字段全集）。

    只存请求与调度状态，不存 Secret（§27）：agent 的凭证在提交执行时由
    Connection Profile 现场解析，值从不进入本模型。
    """

    job_id: str
    status: JobStatus = JobStatus.QUEUED
    request: EvalRunRequest

    requested_by: str = "local"
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    # §33：PREPARING 阶段（健康检查/定义装载）可能还没有 run_id。
    run_id: str | None = None
    gate_verdict: str | None = None

    error: str | None = None
    failure_kind: FailureKind | None = None
    cancel_requested: bool = False

    # §36：浏览器重试/双击不能产生两个相同评测。
    idempotency_key: str | None = None

    progress: JobProgress = Field(default_factory=JobProgress)
