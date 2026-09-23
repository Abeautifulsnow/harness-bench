"""Eval Orchestrator（PRD §5/§85 + Spec V2.1.1 §2.4/§3/§6.1/§7.4）。

1 turn = 同一 session 上的一次 run 调用；每个 iteration：prepare fixture →
create session → 逐轮 run → teardown，iteration 之间不共享任何状态（Spec §2.4）。
失败语义（PRD §46）：Agent 超时/错误 → AGENT_FAILURE；endpoint/SSE 断裂 →
INFRA_FAILURE；Judge 异常 → EVALUATION_FAILURE。ERROR 轮使 Run 进入 partial。

并发：Agent 阶段持有 agent 并发槽位，Judge 阶段不持有（PRD §86 的分离要求）。
"""

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_eval import __version__
from agent_eval.adapters import open_adapter
from agent_eval.adapters.base import (
    AgentAdapter,
    AgentRequest,
    AgentSession,
    SessionContext,
)
from agent_eval.errors import (
    EXIT_GATE_FAIL,
    EXIT_INFRA,
    EXIT_OK,
    InfraError,
    InvalidCallError,
)
from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter
from agent_eval.evaluators.native import (
    EvalScope,
    evaluate_assertions,
    synthesize_platform_verdicts,
)
from agent_eval.evaluators.registry import (
    provider_for,
    resolve_metric,
    scan_unsupported_assertions,
)
from agent_eval.fixtures.base import FixtureHandle, get_provider
from agent_eval.ids import new_id
from agent_eval.loading.loader import (
    load_benchmark,
    load_dataset,
    load_profile,
    load_suites,
    resolve_cases,
)
from agent_eval.models.benchmark import BenchmarkDef, DatasetInfo
from agent_eval.models.case import Case
from agent_eval.models.events import TraceEvent
from agent_eval.models.profile import MetricProfile, MetricSpec
from agent_eval.models.results import (
    CaseRunResult,
    CaseStability,
    CaseStatus,
    MetricResultModel,
    ToolCallRecord,
    TurnResult,
)
from agent_eval.models.run import FailureSemantics, RunMetadata, RunStatus
from agent_eval.models.spans import SpanTree
from agent_eval.regression.stability import compute_stability
from agent_eval.reports.report import (
    build_report,
    compute_verdict,
    render_summary,
    write_report,
)
from agent_eval.storage.run_store import RunStore, _safe
from agent_eval.trace.builder import TraceBuilder

INFRA_RETRIES = 2  # PRD §87 Infrastructure Retry（会话建立阶段）


@dataclass
class RunConfig:
    evals_root: Path
    fixtures_root: Path
    data_root: Path
    benchmark: str
    agent_endpoint: str = "fake://"
    profile: str | None = None
    repeat: int | None = None
    concurrency: int = 4
    tag_filter: list[str] = field(default_factory=list)
    baseline_policy: str | None = None
    baseline_run_id: str | None = None
    no_judge: bool = False
    timeout: float | None = None
    save_trace: bool = True


@dataclass
class RunOutcome:
    run_id: str
    status: str
    verdict: str
    exit_code: int
    report: dict


@dataclass
class _ResolvedProfile:
    profile: MetricProfile
    judge_specs: list[MetricSpec]


@dataclass
class _CaseContext:
    resolved: dict[str, _ResolvedProfile]
    case_profile: dict[str, str]
    judge: DeepEvalCapabilityAdapter
    judge_sem: asyncio.Semaphore
    capabilities: dict[str, bool]
    degradations: dict[str, str]


@dataclass
class _AgentPhase:
    """Agent 执行阶段的产物：交给 judge 阶段，但不占用 agent 并发槽位。"""

    result: CaseRunResult
    tree: SpanTree | None
    scope: EvalScope | None
    resolved: _ResolvedProfile
    completed: bool  # False = 已判定为 ERROR/INFRA，judge 阶段应跳过


def _git_info() -> tuple[str | None, str | None, bool | None]:
    def run(args: list[str]) -> str | None:
        try:
            return (
                subprocess.run(
                    ["git", *args],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout.strip()
                or None
            )
        except (OSError, subprocess.SubprocessError):
            return None

    commit = run(["rev-parse", "--short", "HEAD"])
    branch = run(["branch", "--show-current"])
    dirty = bool(run(["status", "--porcelain"])) if commit else None
    return commit, branch, dirty


class Runner:
    def __init__(self, cfg: RunConfig) -> None:
        self.cfg = cfg
        self.store = RunStore(cfg.data_root / "runs")
        self.adapter: AgentAdapter = open_adapter(cfg.agent_endpoint)

    # ------------------------------------------------------------------ entry

    async def run(self, on_run_created: Callable[[str], None] | None = None) -> RunOutcome:
        cfg = self.cfg
        benchmark = load_benchmark(cfg.evals_root, cfg.benchmark)
        info, cases = load_dataset(cfg.evals_root, benchmark.dataset)
        suites = load_suites(cfg.evals_root)
        selected = resolve_cases(benchmark, suites, cases, cfg.tag_filter)

        unsupported = scan_unsupported_assertions(selected)
        if unsupported:
            raise InvalidCallError(
                "cases carry assertions the P0 native evaluator cannot honor: "
                + "; ".join(unsupported)
            )

        case_profile = {
            case.id: (cfg.profile or case.evaluation_profile or benchmark.default_profile)
            for case in selected
        }
        profiles = {
            name: load_profile(cfg.evals_root, name) for name in sorted(set(case_profile.values()))
        }
        ctx = self._resolve_profiles(profiles, case_profile)

        health = await self.adapter.health_check()
        if not health.ok:
            raise InfraError(f"agent endpoint unhealthy: {health.detail}")

        meta = self._build_meta(benchmark, info)
        run_dir = self.store.create_run(meta)
        meta.metric_capability_snapshot = ctx.capabilities
        meta.metric_degradations = ctx.degradations
        meta.status = RunStatus.running
        self.store.save_meta(meta)
        if on_run_created is not None:
            on_run_created(meta.run_id)  # Spec §6.4: run_id 先于任何执行进度落盘

        results: list[CaseRunResult] = []
        agent_sem = asyncio.Semaphore(cfg.concurrency)
        try:
            async with asyncio.TaskGroup() as tg:
                for case in selected:
                    repeat = cfg.repeat or case.execution.repeat
                    for iteration in range(1, repeat + 1):
                        tg.create_task(
                            self._guarded_iteration(case, iteration, ctx, agent_sem, results)
                        )
        except KeyboardInterrupt:
            meta.status = RunStatus.cancelled
            meta.finished_at = _now()
            self.store.save_meta(meta)
            raise

        stabilities: list[CaseStability] = []
        for case_id in sorted({r.case_id for r in results}):
            stabilities.append(
                compute_stability(case_id, [r for r in results if r.case_id == case_id])
            )
        any_error = any(r.status == CaseStatus.ERROR for r in results)
        meta.status = RunStatus.partial if any_error else RunStatus.completed
        meta.finished_at = _now()
        self.store.save_meta(meta)

        report = build_report(meta, results, stabilities)
        write_report(run_dir, report, render_summary(report))
        verdict = compute_verdict(meta, results)
        if verdict == "fail":
            exit_code = EXIT_GATE_FAIL
        elif meta.status == RunStatus.partial:
            exit_code = EXIT_INFRA
        else:
            exit_code = EXIT_OK
        return RunOutcome(meta.run_id, meta.status.value, verdict, exit_code, report)

    # -------------------------------------------------------------- profiles

    def _resolve_profiles(
        self, profiles: dict[str, MetricProfile], case_profile: dict[str, str]
    ) -> _CaseContext:
        needs_judge = any(
            provider_for(spec) == "deepeval"
            for profile in profiles.values()
            for spec in profile.metrics
        )
        probe: dict[str, bool] = {}
        if needs_judge:
            probe = DeepEvalCapabilityAdapter().probe()
            if self.cfg.no_judge:
                probe = {metric_id: False for metric_id in probe}

        resolved: dict[str, _ResolvedProfile] = {}
        capabilities: dict[str, bool] = {}
        degradations: dict[str, str] = {}
        for name, profile in profiles.items():
            caps: dict[str, bool] = dict(probe)
            judge_specs: list[MetricSpec] = []
            for spec in profile.metrics:
                provider = provider_for(spec)
                caps.setdefault(spec.id, provider == "native")
                effective, degraded_from = resolve_metric(spec, caps, self.cfg.no_judge)
                if degraded_from is not None:
                    # 降级目标是 native 断言组时，信息已由 Native Evaluator 覆盖，
                    # 不重复执行，仅记录降级（Spec §7.4）
                    degradations[degraded_from] = effective or ""
                    continue
                if effective is None:
                    continue  # no_judge → skipped，记录于 Run Metadata
                if provider == "deepeval":
                    judge_specs.append(spec)
            capabilities.update(caps)
            resolved[name] = _ResolvedProfile(profile=profile, judge_specs=judge_specs)
        return _CaseContext(
            resolved=resolved,
            case_profile=case_profile,
            judge=DeepEvalCapabilityAdapter(),
            judge_sem=asyncio.Semaphore(
                max((p.judge_concurrency for p in profiles.values()), default=2)
            ),
            capabilities=capabilities,
            degradations=degradations,
        )

    # -------------------------------------------------------------- iteration

    async def _guarded_iteration(
        self,
        case: Case,
        iteration: int,
        ctx: _CaseContext,
        agent_sem: asyncio.Semaphore,
        results: list[CaseRunResult],
    ) -> None:
        """两阶段执行：Agent 阶段持 agent 槽位，Judge 阶段不持（PRD §86 并发分离）。

        Judge 通常比 Agent 慢得多；若把它压在 agent 槽位内，一个等 Judge 的 case
        会挡住其他 case 的 Agent 调用，两个模型仍互相牵制。
        """
        async with agent_sem:
            phase = await self._execute_agent_phase(case, iteration, ctx)
        result = await self._finish_iteration(case, phase, ctx)
        results.append(result)
        self.store.save_case_run(result)

    async def _execute_agent_phase(
        self, case: Case, iteration: int, ctx: _CaseContext
    ) -> _AgentPhase:
        run_id = self.store.run_dir.name  # type: ignore[union-attr]
        result = _new_case_run(case, iteration, run_id)
        resolved = ctx.resolved[ctx.case_profile[case.id]]
        workdir = self.store.run_dir / "artifacts" / _safe(case.id) / f"iter{iteration}"  # type: ignore[union-attr]
        provider = get_provider(case.environment, self.cfg.fixtures_root)
        handle: FixtureHandle | None = None
        try:
            try:
                handle = await provider.prepare(case.environment, workdir)
            except Exception as exc:
                return _AgentPhase(
                    _error(result, FailureSemantics.INFRA, f"fixture prepare failed: {exc}"),
                    None,
                    None,
                    resolved,
                    completed=False,
                )

            session = await self._open_session(case, iteration, result)
            if session is None:
                return _AgentPhase(
                    _error(
                        result,
                        FailureSemantics.INFRA,
                        result.error or "create_session failed",
                    ),
                    None,
                    None,
                    resolved,
                    completed=False,
                )

            turn_results, session_events, run_status = await self._drive_session(
                session, case, result
            )
            builder = TraceBuilder()
            builder.feed_all(session_events)
            span_tree = builder.build()
            if self.cfg.save_trace and session_events:
                key = f"{_safe(case.id)}.iter{iteration}"
                self.store.append_events(key, session_events)
                result.trace_path = str(self.store.run_dir / "traces" / f"{key}.events.jsonl")  # type: ignore[union-attr]

            scope = _session_scope(run_status, turn_results)
            self._evaluate_session(case, scope, turn_results, result)
            if run_status != "success":
                result.failure_semantics = FailureSemantics.AGENT
                result.error = f"agent run finished with status={run_status!r}"
            return _AgentPhase(result, span_tree, scope, resolved, completed=True)
        except InfraError as exc:  # SSE 流断裂等（§6.1：mandatory suite 未完整执行）
            return _AgentPhase(
                _error(result, FailureSemantics.INFRA, str(exc)),
                None,
                None,
                resolved,
                completed=False,
            )
        except Exception as exc:  # 防单 case 崩溃拖垮整个 TaskGroup
            return _AgentPhase(
                _error(result, FailureSemantics.INFRA, f"unexpected runner error: {exc!r}"),
                None,
                None,
                resolved,
                completed=False,
            )
        finally:
            if handle is not None:
                await provider.cleanup(handle)

    async def _open_session(
        self, case: Case, iteration: int, result: CaseRunResult
    ) -> AgentSession | None:
        last_error: InfraError | None = None
        for attempt in range(INFRA_RETRIES + 1):
            try:
                return await self.adapter.create_session(
                    SessionContext(eval_run_id=result.run_id, case_id=case.id, iteration=iteration)
                )
            except InfraError as exc:
                last_error = exc
                if attempt < INFRA_RETRIES:
                    await asyncio.sleep(1.0 * (attempt + 1))
        result.error = f"create_session failed: {last_error}"
        return None

    async def _drive_session(
        self, session: AgentSession, case: Case, result: CaseRunResult
    ) -> tuple[list[TurnResult], list[TraceEvent], str]:
        """逐轮驱动一次 session，返回 (轮结果, 事件流, run 状态)。"""
        total_timeout = self.cfg.timeout or case.execution.timeout
        turn_results: list[TurnResult] = []
        session_events: list[TraceEvent] = []
        run_status = "success"
        try:
            async with asyncio.timeout(total_timeout):
                for index, message in enumerate(case.input.messages(), start=1):
                    turn, events, failed = await self._run_turn(
                        session, case, index, message, result.id
                    )
                    turn_results.append(turn)
                    session_events.extend(events)
                    if failed:
                        run_status = "error"
                        break
                    if turn.status == "timeout":
                        run_status = "timeout"
                        await self.adapter.cancel(session)
                        break
        except TimeoutError:
            run_status = "timeout"
            await self.adapter.cancel(session)
        return turn_results, session_events, run_status

    def _evaluate_session(
        self,
        case: Case,
        scope: EvalScope,
        turn_results: list[TurnResult],
        result: CaseRunResult,
    ) -> None:
        """session 级评测：两个挂载点（case / final）+ 平台级判定（Spec §2.2/§2.4）。"""
        for mount_name, assertion in case.session_assertions():
            result.metric_results.extend(
                evaluate_assertions(assertion, scope, result.id, mount=mount_name)
            )
        result.metric_results.extend(synthesize_platform_verdicts(scope, result.id))
        result.final_output = scope.final_output
        result.tool_calls = scope.tool_calls
        result.latency_ms = scope.latency_ms
        result.token_count = scope.tokens
        result.turn_results = turn_results

    async def _finish_iteration(
        self, case: Case, phase: _AgentPhase, ctx: _CaseContext
    ) -> CaseRunResult:
        """Judge 阶段 + 终判：不占用 agent 并发槽位。"""
        result = phase.result
        if not phase.completed:
            return result
        judge_error = await self._run_judge_metrics(
            case, phase.resolved, ctx, phase.tree, phase.scope, result
        )
        if judge_error:
            return _error(result, FailureSemantics.EVALUATION, judge_error)
        # turn 级判定参与终判（Spec §2.2：挂载点之间 AND），故遍历 all_metric_results
        if any(metric.verdict == "error" for metric in result.all_metric_results):
            return _error(result, FailureSemantics.EVALUATION, "native evaluator error verdict")
        result.status = CaseStatus.FAIL if result.blocking_failed else CaseStatus.PASS
        return result

    async def _run_turn(
        self, session: AgentSession, case: Case, index: int, message: str, case_run_id: str
    ) -> tuple[TurnResult, list[TraceEvent], bool]:
        turn_spec = case.input.turns[index - 1] if case.input.type == "multi_turn" else None
        per_turn_timeout = (
            turn_spec.timeout if turn_spec is not None and turn_spec.timeout else None
        ) or case.execution.timeout
        builder = TraceBuilder()
        events: list[TraceEvent] = []
        output: str | None = None
        finished_status: str | None = None
        agent_failed = False
        status = "ok"
        error: str | None = None
        try:
            async with asyncio.timeout(per_turn_timeout):
                async for event in self.adapter.run(session, AgentRequest(message=message)):
                    builder.feed(event)
                    events.append(event)
                    if event.type == "error":
                        agent_failed = True
                        error = str(event.data.get("message", "agent error"))
                    if event.type == "run.finished":
                        output = event.data.get("output", output)
                        finished_status = str(event.data.get("status", "success"))
        except TimeoutError:
            status = "timeout"
        if finished_status is None and status == "ok":
            # 流提前结束、没有 run.finished：协议违约，按 agent 失败处理
            agent_failed = True
            status = "error"
            error = "agent stream ended without run.finished"

        tree = builder.build()
        root = next((span for span in tree.spans if span.parent_span_id is None), None)
        latency_ms = 0
        if root is not None and root.finished_at is not None:
            latency_ms = int((root.finished_at - root.started_at).total_seconds() * 1000)
        tool_calls = [
            ToolCallRecord(name=span.name, arguments={}, status=span.attributes.get("tool_status"))
            for span in tree.find("tool")
        ]
        turn = TurnResult(
            index=index,
            output=output,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
            tokens=tree.token_count(),
            status=status,
            error=error,
        )
        if agent_failed or finished_status == "error":
            agent_failed = True
            turn.status = "error"
        # turn 级 expect：仅由 Native Evaluator 消费（Spec §2.5）
        if turn_spec is not None and turn_spec.expect is not None and status == "ok":
            scope = EvalScope(
                run_status=finished_status or ("error" if agent_failed else "success"),
                final_output=output,
                tool_calls=tool_calls,
                latency_ms=turn.latency_ms,
                tokens=turn.tokens,
            )
            turn.metric_results = evaluate_assertions(
                turn_spec.expect,
                scope,
                case_run_id,
                turn_index=index,
                mount=f"turn[{index}]",
            )
        return turn, events, agent_failed

    async def _run_judge_metrics(
        self,
        case: Case,
        resolved: _ResolvedProfile,
        ctx: _CaseContext,
        tree: SpanTree | None,
        scope: EvalScope | None,
        result: CaseRunResult,
    ) -> str:
        if not resolved.judge_specs or tree is None or scope is None:
            return ""
        trace = ctx.judge.convert(
            case,
            tree,
            final_output=scope.final_output,
            latency_ms=scope.latency_ms,
            tokens=scope.tokens,
        )
        for spec in resolved.judge_specs:
            try:
                async with ctx.judge_sem:
                    score, reason = await ctx.judge.evaluate(spec.id, spec.threshold, trace)
            except Exception as exc:  # Judge 失败 ≠ Agent 失败（PRD §46）
                return f"judge metric '{spec.id}' failed: {exc}"
            result.metric_results.append(
                MetricResultModel(
                    id=new_id("mr"),
                    case_run_id=result.id,
                    metric=spec.id,
                    evaluator="deepeval",
                    score=score,
                    threshold=spec.threshold,
                    verdict="pass" if score >= (spec.threshold or 0.0) else "fail",
                    blocking=spec.blocking,
                    reason=reason,
                    metadata={"deepeval_version": ctx.judge.version()},
                )
            )
        return ""

    # ---------------------------------------------------------------- helpers

    def _build_meta(self, benchmark: BenchmarkDef, info: DatasetInfo) -> RunMetadata:
        commit, branch, dirty = _git_info()
        return RunMetadata(
            run_id=new_id("run"),
            benchmark_id=benchmark.name,
            dataset_id=info.id,
            dataset_version=info.version,
            dataset_hash=info.hash,
            profile=self.cfg.profile or "mixed",
            agent_endpoint=self.cfg.agent_endpoint,
            git_commit=commit,
            git_branch=branch,
            git_dirty=dirty,
            deepeval_version=DeepEvalCapabilityAdapter().version(),
            eval_platform_version=__version__,
            status=RunStatus.queued,
            baseline_policy=self.cfg.baseline_policy,
            baseline_run_id=self.cfg.baseline_run_id,
            baseline_mode=self.cfg.baseline_policy or "NO_BASELINE",
            no_judge=self.cfg.no_judge,
            cli_params={
                "repeat": self.cfg.repeat,
                "concurrency": self.cfg.concurrency,
                "tag_filter": self.cfg.tag_filter,
                "timeout": self.cfg.timeout,
                "save_trace": self.cfg.save_trace,
            },
        )


def _new_case_run(case: Case, iteration: int, run_id: str) -> CaseRunResult:
    return CaseRunResult(
        id=new_id("cr"),
        run_id=run_id,
        case_id=case.id,
        case_version=case.version,
        iteration=iteration,
    )


def _session_scope(run_status: str, turn_results: list[TurnResult]) -> EvalScope:
    """session 聚合口径（Spec §2.4）：output=最终轮，其余按 session 总量。"""
    return EvalScope(
        run_status=run_status,
        final_output=turn_results[-1].output if turn_results else None,
        tool_calls=[tool for turn in turn_results for tool in turn.tool_calls],
        latency_ms=sum(turn.latency_ms for turn in turn_results),
        tokens=sum(turn.tokens for turn in turn_results),
    )


def _error(result: CaseRunResult, semantics: FailureSemantics, msg: str) -> CaseRunResult:
    result.status = CaseStatus.ERROR
    result.failure_semantics = semantics
    result.error = msg
    return result


def _now() -> datetime:
    return datetime.now().astimezone()
