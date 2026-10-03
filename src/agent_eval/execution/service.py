"""EvalRunService（设计文档 §20/§21）：Router 与 Runner 之间的唯一编排层。

约束：
- FastAPI Router 不直接操作 Runner；CLI / Web / 未来的 scheduler 都应走到这里；
- Runner 仍是唯一评测执行核心（§20），本层只决定"何时跑、能不能跑"；
- 服务端限制（§13：repeat/agent_concurrency 上限、定义存在性、Connection 可用性）
  全部在这里强制——不信任任何浏览器校验；
- Secret 现场解析（§27）：请求模型里只有 profile 名，凭证在提交执行时才从
  env 读出并注入 RunConfig，从不进 Job 记录、从不回浏览器。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from agent_eval.errors import AgentEvalError, EvaluationInfraError, InfraError, InvalidCallError
from agent_eval.execution import notifier
from agent_eval.execution.connections import (
    AgentConnectionProfile,
    load_agent_connections,
)
from agent_eval.execution.executor import JobExecutor, LocalJobExecutor
from agent_eval.execution.models import (
    TERMINAL_STATUSES,
    EvalRunJob,
    EvalRunRequest,
    FailureKind,
    JobStatus,
)
from agent_eval.execution.repository import JobRepository
from agent_eval.loading.loader import load_benchmark, load_profile, load_suites
from agent_eval.models.regression import BaselineMode
from agent_eval.runner.runner import DEFAULT_GATE, RunConfig, Runner


class InvalidSubmission(Exception):
    """请求违反服务端限制或引用了不存在的定义 → HTTP 400。"""


class UnknownJob(Exception):
    """job_id 不存在 → HTTP 404。"""


class JobAlreadyFinished(Exception):
    """对终态 Job 请求取消 → HTTP 409。"""


class DuplicateSubmission(Exception):
    """同一 Idempotency-Key 的重复提交（§36）→ HTTP 200 重放既有 Job。"""

    def __init__(self, job: EvalRunJob) -> None:
        self.job = job
        super().__init__(f"duplicate submission: {job.job_id}")


class EvalRunService:
    def __init__(
        self,
        evals_root: Path,
        data_root: Path,
        fixtures_root: Path | None = None,
        *,
        max_running_jobs: int = 2,
        # §13 上限是给"不受信任的浏览器提交"设的；CLI 是本机可信入口，传 None
        # 即解禁（review #I03：不能让 Web 上限悄悄改变 CLI 的既有行为）。
        repeat_cap: int | None = 10,
        agent_concurrency_cap: int | None = 16,
        executor: JobExecutor | None = None,
        reap_orphans: bool = True,
    ) -> None:
        self.evals_root = evals_root
        self.data_root = data_root
        self.fixtures_root = fixtures_root if fixtures_root is not None else Path("fixtures")
        self.repo = JobRepository(data_root / "jobs")
        self._notification_log = data_root / "state" / "notifications.jsonl"
        self.repeat_cap = repeat_cap
        self.agent_concurrency_cap = agent_concurrency_cap
        # §23：执行机制可替换（LocalJobExecutor 之外，未来可换 RemoteWorker 等）；
        # 缺省自建本地执行器。
        self.executor: JobExecutor = executor or LocalJobExecutor(
            max_running_jobs,
            on_cancelled=self._on_executor_cancelled,
            on_error=self._on_executor_error,
        )
        self._jobs: dict[str, EvalRunJob] = {}
        # RLock：submit 持锁期间会经 _persist 落账，可重入避免自锁
        # （faulthandler 抓过的死锁：submit → _persist → 二次加锁）。
        self._lock = threading.RLock()
        # review #I02：收割语义只属于"执行进程的启动"——serve 装配时收割；
        # CLI 每次调用都新建 service，与 Web 共享 data_root 时会把活着的
        # 在跑 Job 误标成 interrupted，因此 CLI 传 reap_orphans=False。
        if reap_orphans:
            self._reap_orphaned_jobs()

    # ---------------------------------------------------------------- queries

    def get(self, job_id: str) -> EvalRunJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            job = self.repo.get(job_id)
        if job is None:
            return None
        if job.status is JobStatus.QUEUED:
            # §25：排队位次是读时事实（随调度变化），不落盘——动态补在出参上。
            job = job.model_copy(update={"queue_position": self._queue_position(job_id)})
        return job

    def _queue_position(self, job_id: str) -> int | None:
        """QUEUED Job 的排队位次：提交序中排在它前面的在途任务数 - 运行槽位 + 1。"""

        in_flight = self.executor.ordered_in_flight()
        if job_id not in in_flight:
            return None
        running = sum(
            1
            for job in self._jobs.values()
            if job.status is JobStatus.RUNNING or job.status is JobStatus.CANCELLING
        )
        ahead = in_flight.index(job_id)
        return max(1, ahead - running + 1)

    def list(self) -> list[EvalRunJob]:
        merged = {job.job_id: job for job in self.repo.list()}
        merged.update(self._jobs)
        return sorted(merged.values(), key=lambda j: j.created_at, reverse=True)

    # --------------------------------------------------------------- commands

    def submit(
        self,
        request: EvalRunRequest,
        requested_by: str = "local",
        idempotency_key: str | None = None,
        notify_webhook: str | None = None,
        notify_token_ref: str | None = None,
        connection_override: AgentConnectionProfile | None = None,
    ) -> EvalRunJob:
        with self._lock:
            if idempotency_key:
                replay = self._find_idempotent(idempotency_key)
                if replay is not None:
                    raise DuplicateSubmission(replay)

            self._validate(request, validate_connection=connection_override is None)
            if connection_override is not None:
                # §45：CLI 的 ad-hoc endpoint（--agent / env）。CLI 是本机可信入口，
                # 与 Web 的注册表约束不同；agent_profile 字段仅作请求记录。
                connection = connection_override
            else:
                connection = self._resolve_connection(request.agent_profile)

            job = EvalRunJob(
                job_id=_new_job_id(),
                request=request,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                notify_webhook=notify_webhook,
                notify_token_ref=notify_token_ref,
            )
            self._persist(job)
            self.executor.submit(job.job_id, lambda: self._execute_job(job.job_id, connection))
            return job

    async def run_sync(
        self,
        request: EvalRunRequest,
        requested_by: str = "cli",
        *,
        agent_endpoint: str | None = None,
        on_run_created: Callable[[str], None] | None = None,
        on_submitted: Callable[[EvalRunJob], None] | None = None,
        poll_seconds: float = 0.2,
    ) -> EvalRunJob:
        """§45 统一入口的同步门面：CLI 提交后原地等待终态。

        与 submit() 同一条校验/账本/执行路径；区别只在调用方要等结果。
        ``agent_endpoint`` 是 CLI 专属的 ad-hoc 被测地址（fake:// 缺省 /
        AGENT_EVAL_AGENT_ENDPOINT / 显式 http URL），不要求注册表里有对应 profile。
        ``on_submitted`` 在 Job 创建后立即回调（review #I01）：让 CLI 在
        KeyboardInterrupt 发生于 asyncio.run 返回之前也能拿到 job 引用去取消。
        """

        connection = None
        if agent_endpoint is not None:
            try:
                connection = AgentConnectionProfile(
                    id="cli-adhoc", display_name="CLI ad-hoc", endpoint=agent_endpoint
                )
            except Exception as exc:
                # 与旧直跑路径对齐：非法 endpoint 是配置错误（exit 3），
                # 不是未捕获崩溃（exit 1）；文案保留旧契约关键词。
                raise InvalidSubmission(
                    f"unsupported agent endpoint {agent_endpoint!r}: "
                    f"expected 'fake://' or an http(s) URL ({exc})"
                ) from exc
        job = self.submit(
            request,
            requested_by=requested_by,
            connection_override=connection,
        )
        if on_submitted is not None:
            on_submitted(job)
        fired = False
        while True:
            current = self.get(job.job_id)
            if current is None:  # 理论不可达（内存 + 仓库双写）；防御死循环
                raise UnknownJob(job.job_id)
            if on_run_created is not None and not fired and current.run_id:
                on_run_created(current.run_id)  # Spec §6.4: run_id 先于任何执行进度落盘
                fired = True
            if current.status in TERMINAL_STATUSES:
                return current
            await asyncio.sleep(poll_seconds)

    def cancel(self, job_id: str) -> EvalRunJob:
        job = self.get(job_id)
        if job is None:
            raise UnknownJob(job_id)
        if job.status in TERMINAL_STATUSES:
            raise JobAlreadyFinished(job_id)
        if job.status is JobStatus.CANCELLING:
            return job  # 幂等：取消请求已受理

        # 排队中：直接落 CANCELLED（任务还压在信号量上，会被一并撤掉）。
        # 执行中：落 CANCELLING，等 Runner 的真实 cancellation 传回再收口（§17）。
        target = JobStatus.CANCELLED if job.status is JobStatus.QUEUED else JobStatus.CANCELLING
        self._apply(job.job_id, status=target, cancel_requested=True)
        self.executor.cancel(job_id)
        return self.get(job_id) or job

    def shutdown(self) -> None:
        self.executor.shutdown()

    # -------------------------------------------------------------- internals

    def _find_idempotent(self, key: str) -> EvalRunJob | None:
        for job in self._jobs.values():
            if job.idempotency_key == key:
                return job
        return self.repo.find_by_idempotency_key(key)

    def _validate(self, request: EvalRunRequest, *, validate_connection: bool = True) -> None:
        """全部服务端限制（§13/§44）。命中即 400，不创建 Job。"""

        if (
            self.repeat_cap is not None
            and request.repeat is not None
            and not (1 <= request.repeat <= self.repeat_cap)
        ):
            raise InvalidSubmission(f"repeat must be within 1..{self.repeat_cap}")
        if (
            self.agent_concurrency_cap is not None
            and request.agent_concurrency is not None
            and not (1 <= request.agent_concurrency <= self.agent_concurrency_cap)
        ):
            raise InvalidSubmission(
                f"agent_concurrency must be within 1..{self.agent_concurrency_cap}"
            )
        if request.gate is not None and request.gate not in {"pr", "main", "release"}:
            raise InvalidSubmission("gate must be one of: pr, main, release")
        if request.judge_skip_policy is not None and request.judge_skip_policy not in {
            "none",
            "skip_blocked",
        }:
            raise InvalidSubmission("judge_skip_policy must be one of: none, skip_blocked")
        if request.timeout is not None and request.timeout <= 0:
            raise InvalidSubmission("timeout must be a positive number of seconds")
        # 定义名会拼进文件路径（loader 的 f"{name}.yaml"）：绝对路径会整体替换
        # Path 拼接、相对路径可穿越出 evals/。提交入口是 V1 唯一的执行面门，
        # 白名单在这里设防，而不是信任 loader。
        _require_identifier("benchmark", request.benchmark)
        if request.profile is not None:
            _require_identifier("profile", request.profile)
        # §31：baseline 策略只能选 Git 里已有的语义；explicit 必须指名 baseline run
        # （Runner 会在执行期报 InvalidCallError，但提交期就拒绝体验更好）。
        if request.baseline_policy is not None:
            allowed = {mode.value for mode in BaselineMode}
            if request.baseline_policy not in allowed:
                raise InvalidSubmission(f"baseline_policy must be one of {sorted(allowed)}")
            if request.baseline_policy == BaselineMode.explicit.value and not request.baseline_run:
                raise InvalidSubmission("baseline_policy=explicit requires baseline_run")

        try:
            load_benchmark(self.evals_root, request.benchmark)
        except InvalidCallError as exc:
            raise InvalidSubmission(f"unknown benchmark '{request.benchmark}'") from exc

        if request.profile is not None:
            try:
                load_profile(self.evals_root, request.profile)
            except InvalidCallError as exc:
                raise InvalidSubmission(f"unknown profile '{request.profile}'") from exc

        if request.suite:
            known = set(load_suites(self.evals_root))
            unknown = [s for s in request.suite if s not in known]
            if unknown:
                raise InvalidSubmission(f"unknown suite(s): {', '.join(sorted(unknown))}")

        if validate_connection:
            self._resolve_connection(request.agent_profile)

    def _resolve_connection(self, profile_id: str) -> AgentConnectionProfile:
        profiles = {p.id: p for p in load_agent_connections(self.evals_root)}
        profile = profiles.get(profile_id)
        if profile is None:
            raise InvalidSubmission(f"unknown agent connection '{profile_id}'")
        if not profile.enabled:
            raise InvalidSubmission(f"agent connection '{profile_id}' is disabled")
        if profile.auth is not None and not profile.secret_resolved():
            # §27：secret 状态是 Configured/Missing/Invalid 三态；Missing 时
            # 提交期就拒绝（400），不创建注定失败的 Job。
            raise InvalidSubmission(
                f"agent connection '{profile_id}': secret "
                f"'{profile.auth.secret_ref}' is not configured"
            )
        return profile

    async def _execute_job(self, job_id: str, connection: AgentConnectionProfile) -> None:
        """Job body：跑在执行 loop 上。状态机的 RUNNING→终态 都从这里落。"""

        job = self._jobs.get(job_id)
        if job is None:  # 进程重启后从仓库恢复的在途 Job 不重跑：直接按中断收口
            return
        if job.cancel_requested:
            return  # 排队取消已经落账，别再启动评测

        request = job.request
        cfg = self._build_run_config(request, connection)

        def on_run_created(run_id: str) -> None:
            self._apply(job_id, run_id=run_id)

        def on_progress(counts: dict[str, int]) -> None:
            current = self._jobs.get(job_id)
            if current is not None:
                self._apply(job_id, progress=current.progress.merged(counts))

        self._apply(
            job_id,
            status=JobStatus.RUNNING,
            started_at=datetime.now().astimezone(),
        )

        runner = Runner(cfg)
        try:
            outcome = await runner.run(on_run_created=on_run_created, on_progress=on_progress)
        except asyncio.CancelledError:
            # Runner 已把 run.json 收口成 cancelled；Job 侧由
            # _on_executor_cancelled 统一落账。这里原样上抛。
            raise
        except AgentEvalError as exc:
            self._apply(
                job_id,
                status=JobStatus.FAILED,
                error=exc.message,
                failure_kind=_failure_kind(exc),
                exit_code=exc.exit_code,
                finished_at=datetime.now().astimezone(),
            )
            return
        except Exception as exc:  # noqa: BLE001 - 未预期崩溃必须收口成 FAILED
            self._apply(
                job_id,
                status=JobStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                failure_kind=FailureKind.INTERNAL,
                exit_code=1,
                finished_at=datetime.now().astimezone(),
            )
            return
        finally:
            close = getattr(runner.adapter, "aclose", None)
            if close is not None:
                # 释放失败不该盖掉真实结局；CancellationError 会照常穿透。
                with contextlib.suppress(Exception):
                    await close()

        self._apply(
            job_id,
            status=JobStatus.SUCCEEDED,
            gate_verdict=outcome.gate.verdict if outcome.gate else None,
            gate=outcome.gate.gate if outcome.gate else None,
            run_status=outcome.status,
            exit_code=outcome.exit_code,
            warnings=[*outcome.aggregate.warnings] if outcome.aggregate else [],
            finished_at=datetime.now().astimezone(),
        )

    def _build_run_config(
        self, request: EvalRunRequest, connection: AgentConnectionProfile
    ) -> RunConfig:
        """EvalRunRequest → RunConfig（§46：CLI 与 Web 共享同一套语义）。"""

        agent_headers = connection.auth_headers() if connection.auth is not None else None
        return RunConfig(
            evals_root=self.evals_root,
            fixtures_root=self.fixtures_root,
            data_root=self.data_root,
            benchmark=request.benchmark,
            agent_endpoint=connection.endpoint,
            agent_headers=agent_headers,
            profile=request.profile,
            repeat=request.repeat,
            concurrency=request.agent_concurrency if request.agent_concurrency else 4,
            tag_filter=list(request.tags),
            baseline_policy=request.baseline_policy,
            baseline_run_id=request.baseline_run,
            suites=list(request.suite or []),
            no_judge=request.no_judge,
            strict_protocol=request.strict_protocol,
            save_trace=request.save_trace,
            save_artifacts=request.save_artifacts,
            gate=request.gate or DEFAULT_GATE,
            judge_skip_policy=request.judge_skip_policy or "none",
            timeout=request.timeout,
            agent_model=request.agent_model,
            agent_version=request.agent_version,
            judge_model=request.judge_model,
            experiment_id=request.experiment_id,
            variant_id=request.variant_id,
        )

    # 状态落账：一律 copy-on-write（读者拿到的快照不会撕开），并同步仓库。
    def _apply(self, job_id: str, **updates: object) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status in TERMINAL_STATUSES:
                # 终态不可复活（review #I01）：cancel() 的 get→_apply 两步之间
                # worker 可能已落 SUCCEEDED/FAILED——迟到的 CANCELLING/progress
                # 一律丢弃，否则 Job 会永久卡在 cancelling（无任务可再取消）。
                return
            current = job.model_copy(update=updates)
            self._jobs[job_id] = current
            self.repo.save(current)
        # V2 Notification：终态 + 声明了 webhook → 尽力通知（失败只留痕，
        # 见 notifier 模块；在锁外发，绝不阻塞状态机）。
        if current.status in TERMINAL_STATUSES and current.notify_webhook:
            notifier.fire_and_forget(current, self._notification_log)

    def _reap_orphaned_jobs(self) -> None:
        """进程重启即丢失执行者（review #S01）：repo 里的非终态 Job 诚实收口。

        V1 是进程内执行器，没有跨进程队列；重启后遗留的 running/queued Job
        不会再有人推进，留在 running 会让详情页永远轮询。
        """

        for job in self.repo.list():
            if job.status in TERMINAL_STATUSES:
                continue
            self.repo.save(
                job.model_copy(
                    update={
                        "status": JobStatus.FAILED,
                        "error": "interrupted by process restart before completion",
                        "failure_kind": FailureKind.INTERNAL,
                        "finished_at": datetime.now().astimezone(),
                    }
                )
            )

    def _persist(self, job: EvalRunJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self.repo.save(job)

    def _on_executor_cancelled(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return  # 排队取消路径已落账，这里只兜执行中被取消的收口
        self._apply(
            job_id,
            status=JobStatus.CANCELLED,
            finished_at=datetime.now().astimezone(),
        )

    def _on_executor_error(self, job_id: str, exc: Exception) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return
        self._apply(
            job_id,
            status=JobStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            failure_kind=FailureKind.INTERNAL,
            finished_at=datetime.now().astimezone(),
        )


def _failure_kind(exc: AgentEvalError) -> FailureKind:
    if isinstance(exc, (InfraError, EvaluationInfraError)):
        return FailureKind.INFRA_FAILURE
    if isinstance(exc, InvalidCallError):
        return FailureKind.INVALID_REQUEST
    return FailureKind.INTERNAL


# 定义名要拼进文件路径（loader 的 f"{name}.yaml"）：绝对路径会整体替换 Path
# 拼接、相对路径可穿越出 evals/。白名单只放行仓库里真实存在的命名形态。
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _require_identifier(kind: str, value: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise InvalidSubmission(f"invalid {kind} name: {value!r}")


def _new_job_id() -> str:
    # 时间有序 + 随机尾：同毫秒内也不撞，排序即创建序。
    return f"job_{int(time.time() * 1000):08x}{uuid.uuid4().hex[:10]}"
