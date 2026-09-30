"""Eval Orchestrator（PRD §5/§85 + Spec V2.1.1 §2.4/§3/§4/§6/§7.4）。

1 turn = 同一 session 上的一次 run 调用；每个 iteration：prepare fixture →
create session → 逐轮 run → teardown，iteration 之间不共享任何状态（Spec §2.4）。
失败语义（PRD §46）：Agent 超时/错误 → AGENT_FAILURE；endpoint/SSE 断裂 →
INFRA_FAILURE；Judge 异常 → EVALUATION_FAILURE。ERROR 轮使 Run 进入 partial。

并发：Agent 阶段持有 agent 并发槽位，Judge 阶段不持有（PRD §86 的分离要求）。

终态产物由同一个 RunAggregate 一次写出（Spec §6.2/§6.3）：
report.json / gate.json / junit.xml / report.html / summary.md。
"""

import asyncio
import dataclasses
import json
import subprocess
import time
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
from agent_eval.errors import InfraError, InvalidCallError, MetricUnavailableError
from agent_eval.evaluators.deepeval_adapter import DeepEvalCapabilityAdapter
from agent_eval.evaluators.native import (
    EvalScope,
    evaluate_assertions,
    synthesize_platform_verdicts,
)
from agent_eval.evaluators.plugin import EvaluationContext
from agent_eval.evaluators.registry import (
    available_providers_for,
    plugin_for,
    provider_for,
    resolve_metric,
    run_plugin,
    scan_unsupported_assertions,
)
from agent_eval.fixtures.base import FixtureHandle, FixtureProvider, get_provider
from agent_eval.fixtures.snapshot import EnvironmentSnapshot, snapshot_from_handle
from agent_eval.ids import new_id
from agent_eval.loading.loader import (
    load_benchmark,
    load_dataset,
    load_profile,
    load_suites,
    resolve_suites,
)
from agent_eval.models.artifacts import SnapshotUnavailable
from agent_eval.models.benchmark import BenchmarkDef, DatasetInfo
from agent_eval.models.case import Case
from agent_eval.models.events import EVENT_TYPES, TraceEvent
from agent_eval.models.profile import MetricProfile, MetricSpec
from agent_eval.models.regression import Baseline, BaselineMode, GateReport
from agent_eval.models.results import (
    CaseRunResult,
    CaseStatus,
    MetricResultModel,
    ToolCallRecord,
    TurnResult,
)
from agent_eval.models.run import FailureSemantics, RunMetadata, RunStatus
from agent_eval.models.spans import SpanTree
from agent_eval.quality.gates import (
    SECURITY_TAGS,
    GateRules,
    evaluate_gate,
    exit_code_for,
    load_gate_rules,
)
from agent_eval.regression.compare import compare_runs
from agent_eval.reports.aggregate import RunAggregate, build_aggregate
from agent_eval.reports.cost import PricingTable
from agent_eval.reports.html import render_html_safe
from agent_eval.reports.report import write_reports
from agent_eval.runner.artifacts import collect_artifacts, trace_artifact_record
from agent_eval.security.evaluator import evaluate_security
from agent_eval.storage.analytics import Analytics
from agent_eval.storage.baseline_store import BaselineStore
from agent_eval.storage.run_store import RunStore, _safe
from agent_eval.trace.builder import TraceBuilder

INFRA_RETRIES = 2  # PRD §87 Infrastructure Retry（会话建立阶段）
DEFAULT_GATE = "pr"
# session 级兜底超时相对预算的余量（秒）：正常路径下每一轮都按剩余预算自行收场，
# 这一层只防病理情况，余量留给超时后的 cancel 与收尾落盘。
_SESSION_TIMEOUT_GRACE = 30.0


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
    gate: str = DEFAULT_GATE
    # PRD §108: suites a gate run must execute; None = use the benchmark's own list.
    suites: list[str] = field(default_factory=list)
    no_judge: bool = False
    # PRD §92 的"按策略跳过部分高成本 Judge"。``none``（缺省）= 现状，judge 全跑；
    # ``skip_blocked`` = case 已被阻断性失败判死时跳过该 case 的**非阻断** judge
    # 指标——判定已定，judge 只会花钱不会改结论（保守双条件见 _judge_skip_reason）。
    judge_skip_policy: str = "none"
    timeout: float | None = None
    save_trace: bool = True
    # PRD §90：case 级产物（workdir 变更 / db dump / trace 索引）。
    # 默认开启——失败现场的可复原性是默认能力，不是可选项。
    save_artifacts: bool = True
    experiment_id: str | None = None
    variant_id: str | None = None
    agent_model: str | None = None
    judge_model: str | None = None
    # PRD §30/§109.3：agent version 是可复现信息的一项。Agent 接入协议（PRD §7.1）
    # 的 /health 只约定 ``status``，平台无法自行探测，因此由调用方显式声明；
    # 未声明时保持 None（记录"未知"比编一个值诚实）。
    agent_version: str | None = None
    # E1：协议词汇校验升级开关（未知事件类型 → exit 2）。缺省 warn（只计数留痕）；
    # profile 的 strict_protocol 与它取或——CLI 是开发期显式开 strict 的入口，
    # 收尾档（nightly/strict profile）在 YAML 里常开。
    strict_protocol: bool = False

    @property
    def state_root(self) -> Path:
        return self.data_root / "state"

    @property
    def runs_root(self) -> Path:
        return self.data_root / "runs"


@dataclass
class RunOutcome:
    run_id: str
    status: str
    verdict: str
    exit_code: int
    report: dict
    gate: GateReport | None = None
    aggregate: RunAggregate | None = None
    artifacts: dict[str, str] = field(default_factory=dict)


@dataclass
class _ResolvedProfile:
    profile: MetricProfile
    judge_specs: list[MetricSpec]
    harness_specs: list[MetricSpec]  # PRD §43/§44 的确定性插件（不占 judge 槽位）


@dataclass
class _CaseContext:
    resolved: dict[str, _ResolvedProfile]
    case_profile: dict[str, str]
    case_by_id: dict[str, Case]
    judge: DeepEvalCapabilityAdapter
    judge_sem: asyncio.Semaphore
    capabilities: dict[str, bool]
    degradations: dict[str, str]
    # A2/E2：SUT 观测面能力表（事件名 → bool，health 阶段整份上报），随
    # EvaluationContext 进插件求值；留痕由 run() 并入 metric_capability_snapshot。
    observation_surface: dict[str, bool] = field(default_factory=dict)
    # PRD §91：Judge Model 与 Agent Model 分离。仅落账是不够的——必须真的传进
    # SDK，否则 ``--judge-model`` 只是一个标签，judge 仍走 SDK 默认模型。
    judge_model: str | None = None


@dataclass
class _AgentPhase:
    """Agent 执行阶段的产物：交给评测阶段，但不占用 agent 并发槽位。"""

    result: CaseRunResult
    tree: SpanTree | None
    scope: EvalScope | None
    resolved: _ResolvedProfile
    completed: bool  # False = 已判定为 ERROR/INFRA，评测阶段应跳过
    events: list[TraceEvent] = field(default_factory=list)


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
        self.store = RunStore(cfg.runs_root)
        self.baselines = BaselineStore(cfg.state_root, cfg.runs_root)
        self.pricing = PricingTable.load(cfg.evals_root)
        self.adapter: AgentAdapter = open_adapter(cfg.agent_endpoint)
        # run 级执行期状态（E1/A1/A3）：并发 TaskGroup 内只做事件级累加，
        # 无跨 await 的读改写，asyncio 单线程语义下无需加锁。
        self._protocol_violations: dict[str, int] = {}
        self._run_warnings: list[str] = []
        self._usage_scopes: list[str] = []
        self._sut_agent_model: str | None = None
        self._strict_protocol = False

    # ------------------------------------------------------------------ entry

    async def run(self, on_run_created: Callable[[str], None] | None = None) -> RunOutcome:
        cfg = self.cfg
        benchmark = load_benchmark(cfg.evals_root, cfg.benchmark)
        info, cases = load_dataset(cfg.evals_root, benchmark.dataset)
        suites = load_suites(cfg.evals_root)
        rules = load_gate_rules(cfg.evals_root, cfg.gate)
        # PRD §108: --suite replaces the selection; otherwise the benchmark's own suites
        # are widened by the gate's required ones (a release run must actually execute
        # golden/regression/security/core, not merely be judged as if it had).
        suite_filter = list(cfg.suites)
        if not suite_filter:
            suite_filter = list(dict.fromkeys([*benchmark.suites, *rules.suites]))
        selected, suites_covered = resolve_suites(
            benchmark, suites, cases, suite_filter, cfg.tag_filter
        )

        unsupported = scan_unsupported_assertions(selected)
        if unsupported:
            raise InvalidCallError(
                "cases carry assertions the native evaluator cannot honor: "
                + "; ".join(unsupported)
            )

        case_profile = {
            case.id: (cfg.profile or case.evaluation_profile or benchmark.default_profile)
            for case in selected
        }
        profiles = {
            name: load_profile(cfg.evals_root, name) for name in sorted(set(case_profile.values()))
        }
        # A2 修订（阶段顺序）：health（含能力探测）先于 metric 解析——观测面表是
        # run 级事实，必须整份一次拿到、且在 per-case 执行前到位；放 create_session
        # 意味着每个 case 各报一次、会话创建失败的 case 干脆没有声明。
        # 与 DeepEvalCapabilityAdapter.probe() 的时机同构。顺带修正一处旧时序：
        # 一次注定失败的 run 不再先解析基线。
        health = await self.adapter.health_check()
        if not health.ok:
            raise InfraError(f"agent endpoint unhealthy: {health.detail}")
        self._sut_agent_model = health.agent_model  # A4：SUT 自报的实际生效模型
        self._strict_protocol = self.cfg.strict_protocol or any(
            p.strict_protocol for p in profiles.values()
        )
        ctx = self._resolve_profiles(
            profiles, case_profile, selected, observation_surface=health.observation_surface
        )
        # Spec §17.2：case 级 metric_params 必须有落点——写了个 profile 里不存在的
        # metric id，它不会报错，只会静默不生效，与"永不失败的断言"同类。
        unknown_params = sorted(
            f"{case.id}: metric_params['{metric_id}'] 不在 profile "
            f"'{case_profile[case.id]}' 的 metrics 里"
            for case in selected
            for metric_id in case.metric_params
            if metric_id not in {spec.id for spec in profiles[case_profile[case.id]].metrics}
        )
        if unknown_params:
            raise InvalidCallError(
                "case metric_params target metrics the profile does not run: "
                + "; ".join(unknown_params)
            )
        baseline = self._resolve_baseline(benchmark, info, rules, suites_covered)

        meta = self._build_meta(benchmark, info, baseline, suites_covered)
        run_dir = self.store.create_run(meta)
        meta.metric_capability_snapshot = {
            **ctx.capabilities,
            # A2 留痕段：观测面表与 probe() 共用字段，`event:` 前缀区分键空间
            # （裸键 = metric id 能力，event: 键 = PRD §8 事件观测面）。
            **{f"event:{name}": flag for name, flag in health.observation_surface.items()},
        }
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

        any_error = any(r.status == CaseStatus.ERROR for r in results)
        meta.status = RunStatus.partial if any_error else RunStatus.completed
        meta.finished_at = _now()
        # E1/A1/A3 的执行期账目在收尾时并入元数据（计数器活在 Runner 实例上，
        # 聚合阶段从 meta 读取——RunAggregate 是 run 结束后拼的，执行期必须先落账）。
        meta.protocol_violations = dict(self._protocol_violations)
        meta.token_usage_scope = self._run_usage_scope()
        meta.warnings = list(self._run_warnings)
        self.store.save_meta(meta)

        comparison = self._compare_with_baseline(meta, results, baseline, rules)
        aggregate = build_aggregate(meta, results, comparison)
        gate = evaluate_gate(aggregate, rules, comparison)
        artifacts = self._write_artifacts(run_dir, aggregate, gate)
        self._record_analytics(gate)
        return RunOutcome(
            run_id=meta.run_id,
            status=meta.status.value,
            verdict=gate.verdict,
            exit_code=exit_code_for(gate, aggregate),
            report=_read_report(run_dir),
            gate=gate,
            aggregate=aggregate,
            artifacts={name: str(path) for name, path in artifacts.items()},
        )

    # ------------------------------------------------------------- baseline

    def _resolve_baseline(
        self,
        benchmark: BenchmarkDef,
        info: DatasetInfo,
        rules: GateRules,
        suites_covered: dict[str, int] | None = None,
    ) -> Baseline | None:
        """Spec §4.1 默认策略 + §4.5 运行时覆盖；未命中 → NO_BASELINE（§4.3）。"""
        cfg = self.cfg
        if cfg.baseline_run_id:
            meta_path = self.store.run_dir_for(cfg.baseline_run_id) / "run.json"
            if not meta_path.is_file():
                raise InvalidCallError(f"baseline run not found: {cfg.baseline_run_id}")
            return Baseline(
                id=f"bs-adhoc-{cfg.baseline_run_id}",
                benchmark_id=benchmark.name,
                dataset_version=info.version,
                mode=BaselineMode.explicit,
                pinned_run_id=cfg.baseline_run_id,
                note="runtime --baseline-run override",
            )
        policy = cfg.baseline_policy
        if policy is None:
            if rules.gate == "release":
                policy = BaselineMode.release.value
            elif cfg.experiment_id:
                policy = BaselineMode.explicit.value
            else:
                policy = BaselineMode.main_latest.value
        if policy == BaselineMode.no_baseline.value:
            return None
        if policy == BaselineMode.explicit.value:
            raise InvalidCallError(
                "baseline policy 'explicit' requires --baseline-run <run-id> (Spec §4.5)"
            )
        try:
            mode = BaselineMode(policy)
        except ValueError as exc:
            raise InvalidCallError(
                f"unknown baseline policy '{policy}' "
                f"(expected explicit | release | main-latest | NO_BASELINE)"
            ) from exc
        # ROADMAP「发现的 2」：main-latest 的候选必须与本次 run 的套件组成全等，
        # 否则 run 级均值在两个不同 case 集合之间作差。显式/release pin 是人的决定，
        # 不在解析期拦——但 compare_runs 仍会按同一原则判为 invalid（原因可见）。
        # agent_endpoint 同理：候选必须与本次 run 接入类型相同（联调实测 2026-09-30
        # ——本机 `fake://` 冒烟的历史 run 曾被选成真实 SUT 首个 run 的基线）。
        resolved = self.baselines.resolve(
            benchmark.name,
            info.version,
            mode,
            suites_covered=suites_covered,
            agent_endpoint=self.cfg.agent_endpoint,
        )
        if resolved is None and rules.gate == "release":
            # Spec §4.1: Release Gate 缺省时 fail-fast，不得退化为绝对阈值静默通过
            raise InvalidCallError(
                f"Release Gate 需要显式 pin 的 release baseline，"
                f"但 benchmark '{benchmark.name}' dataset {info.version} 无 release pin"
            )
        return resolved

    def _compare_with_baseline(
        self,
        meta: RunMetadata,
        results: list[CaseRunResult],
        baseline: Baseline | None,
        rules: GateRules,
    ):
        if baseline is None or baseline.pinned_run_id is None:
            meta.baseline_mode = BaselineMode.no_baseline.value
            meta.baseline_reason = "no qualifying baseline run for this dataset_version"
            self.store.save_meta(meta)
            return None
        try:
            base_meta, base_results = self.store.load_run(baseline.pinned_run_id)
        except InvalidCallError as exc:
            meta.baseline_reason = f"baseline run unreadable: {exc.message}"
            self.store.save_meta(meta)
            return None
        meta.baseline_mode = baseline.mode.value
        meta.baseline_run_id = baseline.pinned_run_id
        meta.baseline_reason = None
        self.store.save_meta(meta)
        thresholds = {
            "tool_calls": _num(rules.tool_calls.get("max_regression_percent")),
            "tokens": _num(rules.tokens.get("max_regression_percent")),
            "latency_ms": _num(rules.latency.get("max_regression_percent")),
        }
        return compare_runs(
            base_meta,
            base_results,
            meta,
            results,
            performance_thresholds={k: v for k, v in thresholds.items() if v is not None},
            trace_loader=lambda case_run: self._load_spans(base_meta.run_id, case_run),
        )

    def _load_spans(self, run_id: str, candidate: CaseRunResult) -> dict[str, list]:
        """两侧 span 由 Raw Trace 重放（PRD §56 需要 llm/subagent/retry 计数）。"""
        out: dict[str, list] = {}
        for side, rid in (("baseline", run_id), ("candidate", candidate.run_id)):
            key = f"{_safe(candidate.case_id)}.iter{candidate.iteration}"
            events = self.store.load_events(rid, key)
            if not events:
                out[side] = []
                continue
            builder = TraceBuilder()
            builder.feed_all(events)
            out[side] = builder.build().spans
        return out

    # -------------------------------------------------------------- profiles

    def _resolve_profiles(
        self,
        profiles: dict[str, MetricProfile],
        case_profile: dict[str, str],
        selected: list[Case],
        observation_surface: dict[str, bool] | None = None,
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
            harness_specs: list[MetricSpec] = []
            for spec in profile.metrics:
                provider = provider_for(spec)
                caps.setdefault(spec.id, available_providers_for(spec))
                effective, degraded_from = resolve_metric(spec, caps, self.cfg.no_judge)
                if degraded_from is not None:
                    degradations[degraded_from] = effective or ""
                    continue
                if effective is None:
                    continue  # no_judge → skipped，记录于 Run Metadata
                if provider == "deepeval":
                    judge_specs.append(spec)
                elif provider == "harness":
                    # 插件在注册表里缺席时，注册表就查不到它的 default_threshold
                    if plugin_for(spec.id) is None:
                        raise MetricUnavailableError(
                            f"harness metric '{spec.id}' has no registered plugin "
                            "(PRD §43：先 register_plugin 再进 profile)"
                        )
                    harness_specs.append(spec)
            capabilities.update(caps)
            resolved[name] = _ResolvedProfile(
                profile=profile, judge_specs=judge_specs, harness_specs=harness_specs
            )
        return _CaseContext(
            resolved=resolved,
            case_profile=case_profile,
            case_by_id={case.id: case for case in selected},
            judge=DeepEvalCapabilityAdapter(),
            judge_sem=asyncio.Semaphore(
                max((p.judge_concurrency for p in profiles.values()), default=2)
            ),
            capabilities=capabilities,
            degradations=degradations,
            observation_surface=dict(observation_surface or {}),
            judge_model=self.cfg.judge_model,
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

            session = await self._open_session(case, iteration, result, workdir=workdir)
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
            if session.workdir_accessible is False:
                # A1 修订二/四：同位性回执为"不可达"（如容器路径与宿主不共享）。
                # 静默失效是最坏结局——SUT 拿到访问不了的路径会退化为"环境轴不存在"
                # 且无任何报错；按 InfraError（exit 2）把"配置错"与"agent 失败"分开。
                return _AgentPhase(
                    _error(
                        result,
                        FailureSemantics.INFRA,
                        f"agent reported fixture workdir not accessible: {workdir}",
                    ),
                    None,
                    None,
                    resolved,
                    completed=False,
                )
            if workdir is not None and session.workdir_accessible is None:
                # 回执缺失 = 未知：记账可见，但**不得默认视为可达**——默认可达会把
                # 配置错误伪装成"环境轴不存在"的假信号（A1 验收口径）。
                self._run_warnings.append(
                    f"WORKDIR RECEIPT MISSING: {case.id} iter{iteration} 未回执 workdir "
                    f"可达性（A1：未知 != 可达，环境类断言结论存疑）"
                )

            turn_results, session_events, run_status = await self._drive_session(
                session, case, result
            )
            builder = TraceBuilder()
            builder.feed_all(session_events)
            span_tree = builder.build()
            self._record_usage_scope(span_tree)
            if self.cfg.save_trace and session_events:
                key = f"{_safe(case.id)}.iter{iteration}"
                self.store.append_events(key, session_events)
                result.trace_path = str(self.store.run_dir / "traces" / f"{key}.events.jsonl")  # type: ignore[union-attr]

            # Spec §19：环境快照必须在 provider.cleanup 之前采集（finally 会清理
            # 库文件与工作目录）。它服务 database_state / file_state 两类断言——
            # 那两条判的是"环境变成了什么样"，不是 agent 说了什么。
            # Spec §20.3：database 一并带上，它是 SQL 语义比对的方言来源。
            session_usage = span_tree.usage_totals()
            session_observed = span_tree.usage_observed()
            scope = _session_scope(
                run_status,
                turn_results,
                environment=snapshot_from_handle(handle) if handle is not None else None,
                database=case.environment.database,
                # A3：分量级观测标志——半缺（有输入无输出）时对应分量为 None，
                # 让 max_tokens 判 skipped 而不是拿被低估的总量比阈值。
                input_tokens=(
                    session_usage["input_tokens"] if session_observed["input_tokens"] else None
                ),
                output_tokens=(
                    session_usage["output_tokens"] if session_observed["output_tokens"] else None
                ),
            )
            self._evaluate_session(case, scope, turn_results, result)
            if run_status != "success":
                result.failure_semantics = FailureSemantics.AGENT
                result.error = f"agent run finished with status={run_status!r}"
                result.failure_category = f"agent.{run_status}"
            return _AgentPhase(
                result, span_tree, scope, resolved, completed=True, events=list(session_events)
            )
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
                # Spec §21.2：产物必须在 cleanup **之前**采集——cleanup 会删掉
                # workspace 与库文件，之后就没有现场了。放在 finally 而不是成功
                # 路径上，是为了让 infra error 的现场同样可复原（PRD §90 的
                # "失败现场仍可获取"指的是这一类）。
                await self._collect_artifacts(result, provider, handle, workdir)
                await provider.cleanup(handle)

    async def _open_session(
        self, case: Case, iteration: int, result: CaseRunResult, workdir: Path | None = None
    ) -> AgentSession | None:
        last_error: InfraError | None = None
        for attempt in range(INFRA_RETRIES + 1):
            try:
                return await self.adapter.create_session(
                    SessionContext(
                        eval_run_id=result.run_id,
                        case_id=case.id,
                        variant_id=self.cfg.variant_id,
                        iteration=iteration,
                        # A1：fixture 沙箱 handle 在 prepare（本 case 之前执行）时
                        # 就已就绪，这里补上断掉的一环——把环境交给被测方。
                        # 跨进程句柄必须**绝对路径**：SUT 进程的 CWD 与本进程无关
                        # （联调实测：相对路径让回执"不可达"真实触发，A1 修订二生效）。
                        # E4：extra 保持不透明透传，框架只约定键名、不解释语义。
                        extra={"workdir": str(workdir.resolve())} if workdir is not None else {},
                    )
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
        """逐轮驱动一次 session，返回 (轮结果, 事件流, run 状态)。

        两层超时预算（Spec §2.4）的实现方式是**把剩余 session 预算交给每一轮**——
        `min(per-turn, remaining)`，让到期发生在 turn 层、由 `_run_turn` 自己收场。
        它手里已经有这一轮的 builder、事件与用量，所以"超时"能如实产出"跑掉了多少
        token / 调了多少工具 / 花了多久"；外层 `asyncio.timeout` 只留作兜底。

        此前两层预算取同一个值（单轮 case 下 per-turn 就是 session 超时），外层必然
        先到期：在飞的 `_run_turn` 被取消，它局部的 events 随协程一起消失。后果是
        报告上 `total_tokens=0 / avg_tool_calls=0 / avg_latency_ms=0`——**"超时"读起来
        像"什么都没做"**，而回归平台最该看清现场的时刻恰恰是超时（它为什么没跑完？
        在打转吗？）。联调实测：真实 SUT 首跑，超时那条 case 的 token 与工具调用
        全部归零，只有未超时的几条进了 run 级均值。
        """
        total_timeout = self.cfg.timeout or case.execution.timeout
        deadline = time.monotonic() + total_timeout
        turn_results: list[TurnResult] = []
        session_events: list[TraceEvent] = []
        run_status = "success"
        try:
            # 兜底：正常路径下每一轮都在 deadline 内自行收场，这里只防"轮次极多"或
            # 某轮不遵守 deadline 的病理情况；留一点余量让 cancel 与收尾跑完。
            async with asyncio.timeout(total_timeout + _SESSION_TIMEOUT_GRACE):
                for index, message in enumerate(case.input.messages(), start=1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # 预算已在上一轮耗尽：不再起新一轮（起一个必然立刻超时的轮
                        # 只会多出一条空 turn）。
                        run_status = "timeout"
                        await self.adapter.cancel(session)
                        break
                    turn, events, failed = await self._run_turn(
                        session, case, index, message, result.id, remaining
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
        except TimeoutError:  # 兜底到期：此处拿不到在飞那一轮的局部证据
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
        """session 级评测：三个挂载点（case / final / security）+ 平台级判定。"""
        for mount_name, assertion in case.session_assertions():
            result.metric_results.extend(
                evaluate_assertions(assertion, scope, result.id, mount=mount_name)
            )
        result.metric_results.extend(self._security_verdicts(case, scope, result))
        result.metric_results.extend(synthesize_platform_verdicts(scope, result.id))
        result.final_output = scope.final_output
        result.tool_calls = scope.tool_calls
        result.mcp_calls = scope.mcp_calls
        result.command_calls = scope.command_calls
        result.latency_ms = scope.latency_ms
        result.token_count = scope.tokens
        result.input_tokens = sum(turn.input_tokens for turn in turn_results)
        result.output_tokens = sum(turn.output_tokens for turn in turn_results)
        result.cache_tokens = sum(turn.cache_tokens for turn in turn_results)
        costs = [turn.cost for turn in turn_results if turn.cost is not None]
        if costs:
            result.cost = round(sum(costs), 10)
            result.cost_known = True
        result.turn_results = turn_results

    def _security_verdicts(
        self, case: Case, scope: EvalScope, result: CaseRunResult
    ) -> list[MetricResultModel]:
        """PRD §63：安全规则 deterministic，且只在 Case 声明或安全套件中评测。

        无条件产出会让每个普通 case 都带一组"未声明的 pass"，污染 metric 集合；
        完全不产出又会让安全套件失去 Hard Gate。触发条件取显式声明 + 安全标签。

        安全断言在两个 session 挂载点都可声明（Spec §12），逐挂载点评测并在
        metadata 里标注 ``declared_at``，否则"哪个挂载点要求的"会不可考。
        """
        mounts = [
            (name, assertion)
            for name, assertion in case.session_assertions()
            if not assertion.security.is_empty()
        ]
        tagged = bool(SECURITY_TAGS & set(case.tags))
        if not mounts and not tagged:
            return []
        if not mounts:
            # 只有 tag 触发（未声明任何规则）：用空声明跑一遍平台底线规则
            # （危险命令 / 密钥泄漏），这是套件触发时的既有语义。
            mounts = [("expected", case.expected)]
        out: list[MetricResultModel] = []
        for mount_name, assertion in mounts:
            out.extend(
                finding.as_metric(result.id, lambda: new_id("mr"), declared_at=mount_name)
                for finding in evaluate_security(
                    assertion.security,
                    scope.tool_calls,
                    mcp_names=[call.name for call in scope.mcp_calls],
                    command_calls=scope.command_calls,
                    final_output=scope.final_output,
                    case_tags=list(case.tags),
                )
            )
        return out

    async def _collect_artifacts(
        self,
        result: CaseRunResult,
        provider: FixtureProvider,
        handle: FixtureHandle,
        workdir: Path,
    ) -> None:
        """采集 case 级产物并写进 result（PRD §90，Spec §21）。

        三条约束都体现在这一段里：

        1. **在 cleanup 之前调用**（调用点就在 finally 的第一行）；
        2. **不让 run 失败**：快照失败是"这次少一件产物"，不是"这次执行失败"。
           provider 的 snapshot 是对外扩展点（可能是第三方代码），把它写成
           "可能让一次跑完的执行变 ERROR"会让整个 run 的结论被一件附属品污染；
        3. **如实记账**：provider 抛异常 / 声明采不到（SnapshotUnavailable）/
           返回畸形值 / 名字非法 / 写盘失败，五种情况都进 ``artifact_notes``。
           静默跳过会让"少了一件"看起来像"本来就没有"——而这两件事的排查
           方向完全相反。两种异常的前缀也不同：``snapshot unavailable:`` 指向
           "agent 对环境做了什么"，``fixture snapshot failed:`` 指向 provider。
        """
        if not self.cfg.save_artifacts:
            return
        run_dir = self.store.run_dir  # type: ignore[union-attr]
        snapshot: list = []
        try:
            raw = await provider.snapshot(handle)
            if isinstance(raw, list):
                snapshot = raw
            else:
                result.artifact_notes.append(
                    f"fixture snapshot returned {type(raw).__name__}, expected list"
                )
        except SnapshotUnavailable as exc:
            # "该采的这次拿不到"（库被删 / dump 失败）：与 provider 自己坏掉是
            # 两回事，前缀区分开——前者要查 agent 对环境做了什么，后者查 provider。
            result.artifact_notes.append(f"snapshot unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001 — 见第 2 条
            result.artifact_notes.append(f"fixture snapshot failed: {exc!r}")

        records, skipped = collect_artifacts(snapshot, workdir=workdir, run_dir=run_dir, now=_now())
        # Raw Trace 由 trace 子系统写，这里只登记路径：让"这个 case_run 有哪些
        # 现场可看"能从一个地方回答（Spec §21.3）。
        records.extend(trace_artifact_record(result.trace_path, run_dir, _now()))
        result.artifacts.extend(records)
        result.artifact_notes.extend(skipped)

    async def _finish_iteration(
        self, case: Case, phase: _AgentPhase, ctx: _CaseContext
    ) -> CaseRunResult:
        """确定性插件 + Judge 阶段 + 终判：不占用 agent 并发槽位。"""
        result = phase.result
        if not phase.completed:
            return result
        plugin_error = await self._run_harness_plugins(case, phase, result, ctx)
        if plugin_error:
            return _error(result, FailureSemantics.EVALUATION, plugin_error)
        judge_error = await self._run_judge_metrics(
            case, phase.resolved, ctx, phase.tree, phase.scope, result
        )
        if judge_error:
            return _error(result, FailureSemantics.EVALUATION, judge_error)
        self._record_degradations(ctx, result)
        # turn 级判定参与终判（Spec §2.2：挂载点之间 AND），故遍历 all_metric_results
        if any(metric.verdict == "error" for metric in result.all_metric_results):
            return _error(result, FailureSemantics.EVALUATION, "native evaluator error verdict")
        result.status = CaseStatus.FAIL if result.blocking_failed else CaseStatus.PASS
        if result.status == CaseStatus.FAIL:
            result.failure_category = _failure_category(result)
        return result

    async def _run_harness_plugins(
        self, case: Case, phase: _AgentPhase, result: CaseRunResult, ctx: _CaseContext
    ) -> str:
        """PRD §43/§44：harness 插件是过程内确定性判定，与 judge 阶段并列。

        不占 judge 并发槽位、不受 --no-judge 影响（Spec §17.1）。插件抛异常按
        EVALUATION_FAILURE 处理（PRD §46：插件是评测设施，它的失败不是 agent 的失败）。
        """
        specs = phase.resolved.harness_specs
        if not specs:
            return ""
        scope = phase.scope
        context = EvaluationContext(
            case_run_id=result.id,
            case_id=case.id,
            iteration=result.iteration,
            tags=list(case.tags),
            run_status=scope.run_status if scope else "error",
            final_output=scope.final_output if scope else None,
            tool_calls=list(scope.tool_calls) if scope else [],
            mcp_calls=list(scope.mcp_calls) if scope else [],
            command_calls=list(scope.command_calls) if scope else [],
            spans=list(phase.tree.spans) if phase.tree else [],
            events=list(phase.events),
            latency_ms=scope.latency_ms if scope else 0,
            tokens=scope.tokens if scope else 0,
            # A2 判定段：观测面表进插件上下文，required_events 缺失的插件由
            # run_plugin 判 skipped（不走 resolve_metric——它的语义是 fallback/exit 3）
            observation_surface=dict(ctx.observation_surface),
        )
        for spec in specs:
            plugin = plugin_for(spec.id)
            if plugin is None:  # 启动期已校验，这里只防御性兜底
                continue
            # Spec §17.2：插件默认值 < Profile < Case。case 级覆盖是"这条用例的期望
            # 是什么"（该加载哪个 skill、该压几次），Profile 只给平台级默认。
            params = {
                **plugin.default_params,
                **spec.params,
                **case.metric_params.get(spec.id, {}),
            }
            metric_context = dataclasses.replace(
                context,
                params=params,
                threshold=spec.threshold,
                metric_id=spec.id,
            )
            try:
                metric = await run_plugin(plugin, metric_context)
            except Exception as exc:  # 插件崩溃不得静默变成 pass
                return f"harness evaluator '{spec.id}' failed: {exc!r}"
            # 阻断权以 Profile 为准（与 judge metric 同构）：插件只回答"观测说明了什么"，
            # 是否拦门禁是 Gate 策略，同一插件在不同 Profile 下要能调整阻断力度。
            metric.blocking = spec.blocking
            result.metric_results.append(metric)
        return ""

    def _record_degradations(self, ctx: _CaseContext, result: CaseRunResult) -> None:
        """降级链落账（Spec §7.4）：fallback 未真正产出时必须显式记 skipped。

        native fallback（native.output_checks / native.step_ratio ...）只有在 Case
        真的声明了对应断言组时才会产出 MetricResult。没产出却把降级记成"已覆盖"，
        等于静默跳过约束 —— 与 Spec §7.4 禁止的行为同类。
        """
        produced = {metric.metric for metric in result.all_metric_results}
        for original, fallback in ctx.degradations.items():
            if not fallback or fallback in produced:
                continue
            result.metric_results.append(
                MetricResultModel(
                    id=new_id("mr"),
                    case_run_id=result.id,
                    metric=original,
                    evaluator="native",
                    verdict="skipped",
                    blocking=False,
                    reason=(
                        f"degraded to '{fallback}' but the case declares no assertion that "
                        f"produces it (Spec §7.4); 该 metric 未被评测"
                    ),
                    metadata={"degraded_from": original, "fallback": fallback},
                )
            )

    async def _run_turn(
        self,
        session: AgentSession,
        case: Case,
        index: int,
        message: str,
        case_run_id: str,
        session_remaining: float | None = None,
    ) -> tuple[TurnResult, list[TraceEvent], bool]:
        turn_spec = case.input.turns[index - 1] if case.input.type == "multi_turn" else None
        per_turn_timeout = (
            turn_spec.timeout if turn_spec is not None and turn_spec.timeout else None
        ) or case.execution.timeout
        if session_remaining is not None:
            # session 总预算是硬上限：这一轮最多花掉"还剩下的那些"（Spec §2.4 的两层
            # 超时都要真的生效，而不是靠外层兜底抢先取消）。
            per_turn_timeout = min(per_turn_timeout, session_remaining)
        started = time.monotonic()
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
                    if event.type not in EVENT_TYPES:
                        # E1：PRD §8 是闭合词汇表。未知事件类型默认 warn——执行期
                        # 计数、聚合期进 aggregate.warnings（可见不阻断，判不动
                        # verdict）；strict_protocol 档升级 InfraError（exit 2）：
                        # 协议违约让观测面失效，与"基础设施不可靠"同一语义归属。
                        self._protocol_violations[event.type] = (
                            self._protocol_violations.get(event.type, 0) + 1
                        )
                        if self._strict_protocol:
                            raise InfraError(
                                f"protocol violation: unknown event type {event.type!r} "
                                "(PRD §8 闭合词汇表，strict_protocol)"
                            )
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
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if status == "timeout":
            # 超时轮没有 run.finished：builder 会给未收尾的 span 补一个 finish 时刻
            # （它补的是"最后一条事件的时间"，对本轮而言是假的），所以这里不能信
            # span 的时长——用真实墙钟。记 0 或记假值都会让"超时"这个最该看清耗时
            # 的场景反而看不出它打了多久（联调实测）。
            latency_ms = elapsed_ms
        else:
            latency_ms = 0
            if root is not None and root.finished_at is not None:
                latency_ms = int((root.finished_at - root.started_at).total_seconds() * 1000)
        tool_calls = [
            ToolCallRecord(
                name=span.name,
                arguments=span.input if isinstance(span.input, dict) else {},
                status=span.attributes.get("tool_status"),
                # Spec §19.5：sql_result 断言判定的是"查到了什么"——工具的返回
                # payload 落在 span.output（tool.result 的 result/text）。不采集它
                # 就只能看 agent 的自述输出，那是文本不是结果。
                result=span.output,
            )
            for span in tree.find("tool")
        ]
        # Spec §12.1：mcp.* 与 command.* 是独立 span 类型，需要单独收集——
        # 只收 tool 会让 forbidden_mcp / forbidden_command 永远看不到它们。
        mcp_calls = [
            ToolCallRecord(
                name=span.name, arguments=span.input if isinstance(span.input, dict) else {}
            )
            for span in tree.find("mcp")
        ]
        command_calls = [
            ToolCallRecord(name=span.name, exit_code=span.attributes.get("exit_code"))
            for span in tree.find("command")
        ]
        usage = tree.usage_totals()
        turn = TurnResult(
            index=index,
            output=output,
            tool_calls=tool_calls,
            mcp_calls=mcp_calls,
            command_calls=command_calls,
            latency_ms=latency_ms,
            tokens=tree.token_count(),
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_tokens=usage["cache_tokens"],
            cost=self.pricing.cost(
                self.cfg.agent_model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_tokens=usage["cache_tokens"],
            ),
            status=status,
            error=error,
        )
        if agent_failed or finished_status == "error":
            agent_failed = True
            turn.status = "error"
        # turn 级 expect：仅由 Native Evaluator 消费（Spec §2.5）
        if turn_spec is not None and turn_spec.expect is not None and status == "ok":
            turn_observed = tree.usage_observed()
            scope = EvalScope(
                run_status=finished_status or ("error" if agent_failed else "success"),
                final_output=output,
                tool_calls=tool_calls,
                mcp_calls=mcp_calls,
                command_calls=command_calls,
                latency_ms=turn.latency_ms,
                tokens=turn.tokens,
                # A3：turn 级同样带分量观测标志（半缺 → None → max_tokens skipped）
                input_tokens=usage["input_tokens"] if turn_observed["input_tokens"] else None,
                output_tokens=usage["output_tokens"] if turn_observed["output_tokens"] else None,
                # turn 级也带方言：`tool_arguments` 的 semantic 比对在 turn 级同样可用
                database=case.environment.database,
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
        # PRD §92 执行顺序：Native → 明显 Hard Failure → 按策略跳过高成本 Judge。
        # 跳过发生在 convert 之前：连 trace 都不用构造，一分钱不花。
        skip_reason = _judge_skip_reason(result, resolved.judge_specs, self.cfg.judge_skip_policy)
        if skip_reason:
            for spec in resolved.judge_specs:
                result.metric_results.append(
                    MetricResultModel(
                        id=new_id("mr"),
                        case_run_id=result.id,
                        metric=spec.id,
                        evaluator="deepeval",
                        threshold=spec.threshold,
                        verdict="skipped",
                        blocking=False,
                        reason=skip_reason,
                        metadata={"policy": "skip_blocked"},
                    )
                )
            return ""
        trace = ctx.judge.convert(
            case,
            tree,
            final_output=scope.final_output,
            latency_ms=scope.latency_ms,
            cost=scope.cost,
            # 多轮 case：前序轮次的 assistant 输出是 judge 判"是否偏离意图"的背景
            turn_outputs=[turn.output for turn in result.turn_results],
        )
        for spec in resolved.judge_specs:
            try:
                async with ctx.judge_sem:
                    # judge_model 未声明时不传该参数：SDK 走自身默认模型，
                    # 与"显式指定模型"是两种不同的运行事实，不该混为一谈。
                    score, reason = await ctx.judge.evaluate(
                        spec.id,
                        spec.threshold,
                        trace,
                        **({"model": ctx.judge_model} if ctx.judge_model else {}),
                    )
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

    def _build_meta(
        self,
        benchmark: BenchmarkDef,
        info: DatasetInfo,
        baseline: Baseline | None,
        suites_covered: dict[str, int] | None = None,
    ) -> RunMetadata:
        commit, branch, dirty = _git_info()
        return RunMetadata(
            run_id=new_id("run"),
            benchmark_id=benchmark.name,
            dataset_id=info.id,
            dataset_version=info.version,
            dataset_hash=info.hash,
            profile=self.cfg.profile or "mixed",
            suites_covered=suites_covered or {},
            agent_endpoint=self.cfg.agent_endpoint,
            git_commit=commit,
            git_branch=branch,
            git_dirty=dirty,
            # A4：SUT 在 health 上报的"实际生效模型"权威于 CLI 标签（前者是
            # 被测方自己承认在跑的模型）。字段从此是受基线守卫校验的字段，
            # 不是自由标签（compare.py 对两侧不一致判 INVALID）。
            agent_model=self._sut_agent_model or self.cfg.agent_model,
            agent_version=self.cfg.agent_version,
            judge_model=self.cfg.judge_model,
            deepeval_version=DeepEvalCapabilityAdapter().version(),
            eval_platform_version=__version__,
            status=RunStatus.queued,
            experiment_id=self.cfg.experiment_id,
            variant_id=self.cfg.variant_id,
            variant=self.cfg.variant_id,
            baseline_policy=self.cfg.baseline_policy or self._default_policy(),
            baseline_run_id=baseline.pinned_run_id if baseline else None,
            baseline_mode=baseline.mode.value if baseline else BaselineMode.no_baseline.value,
            baseline_reason=(
                None if baseline else "no qualified baseline for this dataset_version"
            ),
            no_judge=self.cfg.no_judge,
            cli_params={
                "repeat": self.cfg.repeat,
                "concurrency": self.cfg.concurrency,
                "tag_filter": self.cfg.tag_filter,
                "timeout": self.cfg.timeout,
                "save_trace": self.cfg.save_trace,
                "gate": self.cfg.gate,
            },
        )

    def _default_policy(self) -> str:
        if self.cfg.baseline_run_id:
            return BaselineMode.explicit.value
        if self.cfg.gate == "release":
            return BaselineMode.release.value
        if self.cfg.experiment_id:
            return BaselineMode.explicit.value
        return BaselineMode.main_latest.value

    def _write_artifacts(
        self, run_dir: Path, aggregate: RunAggregate, gate: GateReport
    ) -> dict[str, Path]:
        return write_reports(run_dir, aggregate, gate, html=render_html_safe(aggregate, gate))

    def _record_analytics(self, gate: GateReport) -> None:
        """DuckDB 是派生层：投影失败不得影响已产出的报告（派生层可随时重建）。"""
        try:
            with Analytics(self.cfg.data_root / "analytics.duckdb") as analytics:
                analytics.rebuild(self.cfg.runs_root, self.cfg.evals_root)
                analytics.record_gate(gate)
        except Exception:  # noqa: BLE001 — 分析层不可用时不掩盖运行结果
            return

    def _record_usage_scope(self, tree: SpanTree) -> None:
        """A3 修订五：按 case 记录用量口径，收尾聚合成 run 级 token_usage_scope。

        conservative 口径：只要有一个 case 是单侧观测（外部流上只报输入侧的 SUT），
        整个 run 的 token 数字就是被低估的，跨 run 比较必须先过口径守卫。
        """
        observed = tree.usage_observed()
        if observed["input_tokens"] and observed["output_tokens"]:
            self._usage_scopes.append("full")
        elif observed["input_tokens"] or observed["output_tokens"]:
            self._usage_scopes.append("partial")
        else:
            self._usage_scopes.append("none")

    def _run_usage_scope(self) -> str | None:
        """run 级口径：full（全部双侧）/ partial（存在单侧观测的 case）/ None（全程无观测）。

        "全程无观测"必须是 None 而不是 partial（联调实测缺陷）：两者都让口径守卫
        拦下比较，但**含义不同**——partial 说"总量被低估，是观察不全"，None 说
        "这次 run 根本没有任何用量事实"。把它们并成一个值，报告上就分不出
        "SUT 只上报输入侧"（协议形态问题）与"这次 run 一个 usage 都没拿到"
        （run 本身没跑起来）。本函数与 `RunMetadata.token_usage_scope` 的字段注释
        是同一份契约，此前实现把全 none 归进 partial，与注释相矛盾。
        """
        if not self._usage_scopes:
            return None
        if all(scope == "full" for scope in self._usage_scopes):
            return "full"
        if all(scope == "none" for scope in self._usage_scopes):
            return None
        return "partial"


def _new_case_run(case: Case, iteration: int, run_id: str) -> CaseRunResult:
    return CaseRunResult(
        id=new_id("cr"),
        run_id=run_id,
        case_id=case.id,
        case_version=case.version,
        case_tags=list(case.tags),
        iteration=iteration,
        # 反范式化的方言声明（Spec §20.3）：semantic / AST diff 需要它，但两侧
        # run 的 compare 阶段只有 CaseRunResult、拿不到 Case。与 case_tags 同理，
        # 事实源仍是 case YAML。
        environment_database=case.environment.database,
    )


def _session_scope(
    run_status: str,
    turn_results: list[TurnResult],
    *,
    environment: EnvironmentSnapshot | None = None,
    database: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> EvalScope:
    """session 聚合口径（Spec §2.4）：output=最终轮，其余按 session 总量。

    ``input_tokens`` / ``output_tokens`` 是 A3 的分量观测值：None = 该分量未观测
    （半缺），由 `max_tokens` 等依赖方判 skipped——聚合层不做"缺了就补 0"。
    """
    costs = [turn.cost for turn in turn_results if turn.cost is not None]
    return EvalScope(
        run_status=run_status,
        final_output=turn_results[-1].output if turn_results else None,
        tool_calls=[tool for turn in turn_results for tool in turn.tool_calls],
        mcp_calls=[call for turn in turn_results for call in turn.mcp_calls],
        command_calls=[call for turn in turn_results for call in turn.command_calls],
        latency_ms=sum(turn.latency_ms for turn in turn_results),
        tokens=sum(turn.tokens for turn in turn_results),
        # PRD §59：一个 turn 有定价就说明整轮有定价；全为 None 时保持 None
        # （不是 0.0）——max_cost 断言靠这个区分"没有定价"与"成本为零"。
        cost=round(sum(costs), 10) if costs else None,
        environment=environment,
        database=database,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _failure_category(result: CaseRunResult) -> str | None:
    """兜底分类：第一个 blocking 失败的 metric id（P3 由 Failure Taxonomy 细化）。"""
    for metric in result.all_metric_results:
        if metric.blocking and metric.verdict in {"fail", "error"}:
            return metric.metric
    return None


def _error(result: CaseRunResult, semantics: FailureSemantics, msg: str) -> CaseRunResult:
    result.status = CaseStatus.ERROR
    result.failure_semantics = semantics
    result.error = msg
    result.failure_category = f"infra.{semantics.value.lower()}"
    return result


def _judge_skip_reason(result: CaseRunResult, judge_specs: list, policy: str) -> str:
    """PRD §92 的"按策略跳过部分高成本 Judge"：返回跳过原因，空串 = 不跳。

    两个保守条件缺一不可，方向都是"不能因省钱改判定"：
    - 只跳**非阻断**的 judge 指标——blocking 的 judge 参与判定，跳过等于改判；
      此时宁可贵也要跑（调用方应改 profile，而不是指望跳过策略省成本）。
    - 只在本次 iteration 已有 blocking fail 时跳——判定已定，judge 只会花钱
      不会改结论；``metric_results`` 此时已含 native / harness / security 的结果
      （_finish_iteration 的执行顺序），security Hard Failure 也在其中（§12.2
      的 blocking=True）。
    """
    if policy != "skip_blocked" or not judge_specs:
        return ""
    if any(spec.blocking for spec in judge_specs):
        return ""
    if not any(m.blocking and m.verdict in {"fail", "error"} for m in result.metric_results):
        return ""
    return "case 已被阻断性失败判死，judge 按策略跳过（PRD §92）"


def _num(value: object) -> float | None:
    try:
        return float(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_report(run_dir: Path) -> dict:
    path = run_dir / "report.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _now() -> datetime:
    return datetime.now().astimezone()
