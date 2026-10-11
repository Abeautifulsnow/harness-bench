"""Execution Plane 域模型（设计文档 §15/§33/§46）。

Job ≠ Run（§33）：Job 是"用户请求执行一次评测"的生命周期载体，Run 是 Runner
实际产生的一次评测。Job 记录放在 ``<data_root>/jobs/``，绝不写进 ``runs/``；
Run 的可复现事实（git commit、dataset version、model、judge model）继续由
run.json 承载（§37 的审计通过 job.run_id 关联，不复制）。

§63（2026-10-10）：Job 增加 ``kind`` 判别位——同一个队列/执行器/账本/SSE 同时
承载 benchmark 评测（``eval``）与生产 Trace 重放（``replay``）。理由见 §63.1：
重放天然是长任务（§14 的同步 HTTP 禁令对它同样成立），而 §21-§25 的 Job 机制
是"唯一执行核心"的载体，另起一套队列只会得到两套取消/进度/审计语义。
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


class JobKind(StrEnum):
    """Job 种类（§63）。两种 Job 走**同一条**调度/取消/SSE 路径，只在执行体分叉。

    判别位落在请求模型上（而不是建两个 Job 类型）：§22 的账本、§25 的队列、
    §17 的取消、§18 的 SSE 全部按 job_id 寻址，与 kind 无关；分成两个类型会
    逼着这些横切能力各写两遍。
    """

    EVAL = "eval"  # benchmark 评测（§16）
    REPLAY = "replay"  # 生产 Trace 重放（§62/§63）


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

    ``kind`` 分叉字段语义（§63）：``eval`` 用 benchmark/suite/profile/repeat 等
    benchmark 侧字段；``replay`` 用 trace_id/case_id/sanitize/replay_timeout_seconds/
    policy。两组的必填项由 ``EvalRunService._validate`` 按 kind 检查——
    模型层不做跨字段条件必填（那会让 CLI 的构造路径与 Web 的解析路径各写一套）。
    """

    # 语义按入口分叉（review #S01）：Web/触发器/调度传 **Agent Connection 的
    # 注册 id**（submit() 据此查注册表解析 endpoint + 凭证）；CLI 传 **ad-hoc
    # endpoint 字符串**（fake:// 或 http(s) URL，经 run_sync 的 connection_override
    # 绕开注册表，本字段仅作请求记录）。除 CLI 外它不是 URL，而是名字。
    agent_profile: str

    kind: JobKind = JobKind.EVAL

    # kind=eval 必填、kind=replay 不用（重放的对象是生产 Trace，不是 benchmark）。
    benchmark: str | None = None
    suite: list[str] | None = None
    profile: str | None = None

    repeat: int | None = None

    # 映射到 RunConfig.concurrency（agent 侧并发，CLI --concurrency）；None = Runner 缺省。
    agent_concurrency: int | None = None

    no_judge: bool = False
    strict_protocol: bool = False
    save_trace: bool = True
    # PRD §90：case 级产物采集（CLI --no-save-artifacts 的反向）。
    save_artifacts: bool = True

    tags: list[str] = Field(default_factory=list)

    baseline_policy: str | None = None
    baseline_run: str | None = None

    # ---- 执行调优字段（§45：CLI 与 Web 共享同一请求模型的全部语义）----
    # gate 规则集（pr | main | release）；None = Runner 缺省 pr。
    gate: str | None = None
    # PRD §92：none | skip_blocked。
    judge_skip_policy: str | None = None
    # 运行期预算覆盖（秒），替换 case 声明的 execution.timeout。
    timeout: float | None = None
    # PRD §30/§109.3 可复现信息与 judge 分离。
    agent_model: str | None = None
    agent_version: str | None = None
    judge_model: str | None = None
    # PRD §22–§25 实验归属（CLI 实验流程使用）。
    experiment_id: str | None = None
    variant_id: str | None = None

    # ---- kind=replay 专用（§63；与 §62 的同步端点共用同一套语义）----
    # 被重放的生产 Trace。多 case 的 trace 必须显式给 case_id（与同步端点同口径：
    # 猜一个 case 等于替用户决定测哪条）。
    trace_id: str | None = None
    case_id: str | None = None
    # 发送前脱敏（§62.2：密钥/email/手机等 → 类型化占位符）；默认开。
    sanitize: bool = True
    # 单次重放的预算（秒）；命中即按 INFRA_FAILURE 收口（与执行体同一语义）。
    # 不叫 timeout 是刻意的：timeout 在 eval 侧是"case 预算覆盖"，语义不同，
    # 共用一个字段会让两条执行体各自解释它。
    replay_timeout_seconds: float = 60.0
    # 可选的 Online Eval policy：给了就对两侧同尺打分，没给就不编造分数。
    policy: str | None = None


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

    # kind 在 Job 上也留一份（与 request.kind 同源）：列表按 kind 过滤、详情页
    # 免于解包 request 就能分支渲染。两处必一致（构造时由 request 带入）。
    kind: JobKind = JobKind.EVAL

    requested_by: str = "local"
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    # §33：PREPARING 阶段（健康检查/定义装载）可能还没有 run_id。
    run_id: str | None = None
    # kind=replay：ReplayStore 的记录 id（比较结论在 GET /production/replays/{id}）。
    # 与 run_id 并列而不是复用：重放**不产生 Run**（它是生产面的一条 trace），
    # 塞进 run_id 会让"这次 Job 评测了哪个 Run"这个语义说谎。
    replay_id: str | None = None

    gate_verdict: str | None = None
    # §45：CLI 直接消费的结果字段（Web 详情页同样可读）。run_status 是 Run 的
    # 生命周期（completed/partial/cancelled/...），区别于 Job 自身的 status。
    run_status: str | None = None
    gate: str | None = None
    exit_code: int | None = None
    warnings: list[str] = Field(default_factory=list)

    error: str | None = None
    failure_kind: FailureKind | None = None
    cancel_requested: bool = False

    # §36：浏览器重试/双击不能产生两个相同评测。
    idempotency_key: str | None = None

    # V2 Notification：终态 webhook。值由提交时的 schedule/trigger 给出；URL 本身
    # 不是凭证，Bearer 凭证以 env var 名（notify_token_ref）现场解析、不落盘。
    notify_webhook: str | None = None
    notify_token_ref: str | None = None

    # §25：排队位次是**读时事实**（随调度实时变化）——动态补在出参上，
    # repository.save 用 exclude 永不落盘，避免留下一份过期的位次快照。
    queue_position: int | None = None

    progress: JobProgress = Field(default_factory=JobProgress)


class ReplayRunRequest(BaseModel):
    """Replay Job 的 API 请求体（§63）。

    只收 replay 侧字段，``kind`` 由服务端固定为 ``replay``——把 kind 暴露给客户端
    会让"提交了 kind=eval 却只给 trace_id"这类请求在生产里变成 400，而它真正的
    语义是"这个端点就是重放"。映射在 ``to_request()`` 里一次完成，执行面看到的
    仍是 §46 的同一个 ``EvalRunRequest``（CLI 与 Web 不各说各话）。
    """

    # extra="forbid"：与 EvalRunRequest（宽松面）有意分歧——重放请求里出现
    # benchmark 这类字段，按"忽略"处理会让客户端的参数错配静默通过（拿重放端点
    # 发评测请求），正是本仓反复拦的"声明了却不生效"。校验层拒绝 → 422。
    model_config = {"extra": "forbid"}

    agent_profile: str
    trace_id: str
    case_id: str | None = None
    sanitize: bool = True
    replay_timeout_seconds: float = 60.0
    policy: str | None = None

    def to_request(self) -> EvalRunRequest:
        return EvalRunRequest(
            agent_profile=self.agent_profile,
            kind=JobKind.REPLAY,
            trace_id=self.trace_id,
            case_id=self.case_id,
            sanitize=self.sanitize,
            replay_timeout_seconds=self.replay_timeout_seconds,
            policy=self.policy,
        )
