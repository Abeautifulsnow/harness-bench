"""Quality / Gate / Review / Security 只读接口（PRD §60–§69/§84）。

Gate 视图的 ``source`` 字段是刻意的：``stored`` = 当时落盘的 gate.json（历史判定），
``replayed`` = 用当前规则集重放（例如规则刚改过）。UI 必须能区分这两者，
否则"改了阈值后重看历史 run"会被误读成"当时的判定就是新阈值下的结论"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from agent_eval.api.deps import WorkspaceDep
from agent_eval.api.schemas import (
    BaselineRow,
    GateRulesetRow,
    GateView,
    RedTeamCoverageRow,
    ReviewList,
    ReviewQueue,
    ReviewQueueRow,
    ReviewRow,
    SecurityPosture,
    SecuritySuiteRow,
)
from agent_eval.errors import AgentEvalError
from agent_eval.loading.loader import load_all_datasets
from agent_eval.quality.gates import DEFAULT_RULES, GATE_KINDS, load_gate_rules
from agent_eval.review.store import QUEUE_REASONS, VERDICTS, queue_candidates
from agent_eval.security.redteam import RED_TEAM_CATEGORIES, classify_cases
from agent_eval.security.suites import list_suites

router = APIRouter(tags=["quality"])


@router.get("/gates/rules", response_model=list[GateRulesetRow], summary="PRD §68 三套 Gate 规则集")
def gate_rules(workspace: WorkspaceDep) -> list[GateRulesetRow]:
    rows: list[GateRulesetRow] = []
    for name in GATE_KINDS:
        try:
            rules = load_gate_rules(workspace.evals_root, name)
        except AgentEvalError:
            continue
        rows.append(
            GateRulesetRow(
                gate=rules.gate,
                strict=rules.strict,
                suites=list(rules.suites),
                thresholds={
                    "task_success": rules.task_success,
                    "tool_calls": rules.tool_calls,
                    "tokens": rules.tokens,
                    "latency": rules.latency,
                    "security": rules.security,
                    "golden": rules.golden,
                },
                hard_failure_categories=list(rules.hard_failure_categories),
            )
        )
    if not rows:
        rows.append(
            GateRulesetRow(
                gate="pr",
                thresholds=dict(DEFAULT_RULES),
            )
        )
    return rows


@router.get("/gates", response_model=list[GateView], summary="Gate 历史（按 run）")
def gate_history(
    workspace: WorkspaceDep,
    benchmark: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[GateView]:
    from agent_eval.api.services import load_view

    out: list[GateView] = []
    metas = workspace.run_store().list_runs()
    metas.sort(key=lambda meta: meta.started_at, reverse=True)
    for meta in metas:
        if benchmark and meta.benchmark_id != benchmark:
            continue
        view = load_view(workspace.run_store(), meta.run_id, workspace.evals_root)
        out.append(_gate_view(view))
        if len(out) >= limit:
            break
    return out


@router.get("/gates/{run_id}", response_model=GateView, summary="PRD §75 Gate 结论与逐条规则")
def gate_of_run(
    run_id: str,
    workspace: WorkspaceDep,
    gate: Annotated[str | None, Query(description=" | ".join(GATE_KINDS))] = None,
) -> GateView:
    from agent_eval.api.services import load_view

    try:
        view = load_view(workspace.run_store(), run_id, workspace.evals_root)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    if gate and gate != "pr":
        rules = load_gate_rules(workspace.evals_root, gate)
        from agent_eval.quality.gates import evaluate_gate

        report = evaluate_gate(view.aggregate(), rules, view.comparison())
        return _gate_view(view, report=report, source="replayed")
    return _gate_view(view)


def _gate_view(view, report=None, source: str | None = None) -> GateView:
    if report is None:
        report, resolved_source = view.gate()
        source = resolved_source
    return GateView(
        run_id=view.meta.run_id,
        gate=report.gate,
        verdict=report.verdict,
        baseline_mode=report.baseline_mode,
        baseline_run_id=report.baseline_run_id,
        rules=[rule.model_dump(mode="json") for rule in report.rules],
        aggregate=report.aggregate,
        notes=list(report.notes),
        exit_code=view.gate_exit_code(report),
        source=source or "replayed",
    )


# ------------------------------------------------------------------ reviews


@router.get("/reviews/options", response_model=dict, summary="PRD §60 结论与入队理由枚举")
def review_options() -> dict:
    return {"verdicts": list(VERDICTS), "queue_reasons": list(QUEUE_REASONS)}


@router.get("/reviews", response_model=ReviewList, summary="PRD §60 Human Review 记录")
def list_reviews(
    workspace: WorkspaceDep,
    run_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query(description="pending")] = None,
) -> ReviewList:
    store = workspace.review_store()
    records = store.list(run_id=run_id, status=status)
    # 机器结论随行给出（Spec §5.4：人工与机器结论并存，不互相覆盖）
    machine: dict[tuple, str] = {}
    runs = {record.get("run_id") for record in records if record.get("run_id")}
    run_stores = workspace.run_store()
    for rid in runs:
        try:
            _, results = run_stores.load_run(rid)
        except AgentEvalError:
            continue
        from agent_eval.review.store import _machine_verdict

        for case_id in {r.case_id for r in results}:
            iterations = [r for r in results if r.case_id == case_id]
            machine[(rid, case_id)] = _machine_verdict(iterations)
    return ReviewList(
        projection="ok",
        run_id=run_id,
        status=status,
        queue_reasons=list(QUEUE_REASONS),
        verdicts=list(VERDICTS),
        total=len(records),
        reviews=[
            ReviewRow(
                id=record.get("id", ""),
                run_id=record.get("run_id", ""),
                case_id=record.get("case_id", ""),
                reviewer=record.get("reviewer", ""),
                verdict=record.get("verdict", ""),
                note=record.get("note", ""),
                queue_reason=record.get("queue_reason"),
                created_at=record.get("created_at"),
                case_run_id=record.get("case_run_id"),
                machine_verdict=machine.get((record.get("run_id"), record.get("case_id"))),
                status="reviewed" if record.get("verdict") else "pending",
            )
            for record in records
        ],
    )


@router.get(
    "/reviews/queue/{run_id}",
    response_model=ReviewQueue,
    summary="PRD §61 Review Queue 候选与入队理由",
)
def review_queue(run_id: str, workspace: WorkspaceDep) -> ReviewQueue:
    store = workspace.run_store()
    try:
        meta, results = store.load_run(run_id)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    candidates = queue_candidates(meta, results)
    rows: list[ReviewQueueRow] = []
    for candidate in candidates:
        iterations = [r for r in results if r.case_id == candidate["case_id"]]
        rows.append(
            ReviewQueueRow(
                case_id=candidate["case_id"],
                queue_reason=candidate["queue_reason"],
                machine_verdict=candidate.get("machine_verdict"),
                iterations=len(iterations),
                pass_rate=round(
                    sum(1 for r in iterations if r.status.value == "PASS") / len(iterations), 6
                )
                if iterations
                else 0.0,
                stability=candidate.get("stability", "UNKNOWN"),
                detail=", ".join(candidate.get("reasons") or []),
            )
        )
    return ReviewQueue(projection="ok", run_id=run_id, total=len(rows), candidates=rows)


# ----------------------------------------------------------------- security


@router.get("/security", response_model=SecurityPosture, summary="PRD §62/§63 安全与红队态势")
def security_posture(
    workspace: WorkspaceDep,
    run_id: Annotated[str | None, Query()] = None,
) -> SecurityPosture:
    root = workspace.evals_root
    # 套件计数与红队覆盖矩阵读的是同一份 case：定义树整树只装一次（Spec §22.12）。
    cases_by_ref, _errors = load_all_datasets(root)
    cases = [case for group in cases_by_ref.values() for case in group]
    suites = [
        SecuritySuiteRow(
            name=summary.name,
            tag=summary.tag,
            cases=summary.cases,
            description=summary.description,
        )
        for summary in list_suites(root, cases_by_ref=cases_by_ref)
    ]
    coverage = _red_team_coverage(cases)
    findings: list[dict] = []
    if run_id:
        from agent_eval.api.services import load_view

        try:
            view = load_view(workspace.run_store(), run_id, workspace.evals_root)
        except AgentEvalError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        findings = [
            {
                "case_id": result.case_id,
                "case_run_id": result.id,
                "iteration": result.iteration,
                "metric": metric.metric,
                "verdict": metric.verdict,
                "blocking": metric.blocking,
                "mount": metric.metadata.get("mount"),
                "reason": metric.reason,
            }
            for result in view.results
            for metric in result.all_metric_results
            if metric.metric.startswith("security.")
        ]
    return SecurityPosture(
        projection="ok",
        run_id=run_id,
        suites=suites,
        coverage=coverage,
        red_team_categories=list(RED_TEAM_CATEGORIES),
        findings=findings,
        failed_findings=sum(1 for finding in findings if finding["verdict"] == "fail"),
    )


def _red_team_coverage(cases: list) -> list[RedTeamCoverageRow]:
    """PRD §62 八类攻击面覆盖矩阵；缺失的一类显式标 covered=false（可见负债）。

    归类复用 ``classify_cases``（选择口径只有一份实现）；调用方为同一请求已经读过
    一次定义树，这里不再装载（Spec §22.12）。
    """
    rows = {category: [] for category in RED_TEAM_CATEGORIES}
    try:
        for item in classify_cases(cases):
            rows.setdefault(item.category, []).append(item.case_id)
    except Exception:  # noqa: BLE001 — 定义树不完整时仍要能显示"完全未覆盖"
        rows = {category: [] for category in RED_TEAM_CATEGORIES}
    return [
        RedTeamCoverageRow(
            category=category,
            cases=len(case_ids),
            case_ids=sorted(case_ids),
            covered=bool(case_ids),
        )
        for category, case_ids in sorted(rows.items())
    ]


# ----------------------------------------------------------------- baselines


@router.get("/baselines", response_model=list[BaselineRow], summary="Spec §4.4 baseline 台账")
def list_baselines(
    workspace: WorkspaceDep,
    benchmark: Annotated[str | None, Query()] = None,
) -> list[BaselineRow]:
    store = workspace.baseline_store()
    records = store.show(benchmark) if benchmark else store.effective()
    return [
        BaselineRow(
            id=record.id,
            benchmark_id=record.benchmark_id,
            dataset_version=record.dataset_version,
            mode=record.mode.value,
            pinned_run_id=record.pinned_run_id,
            pinned_by=record.pinned_by,
            pinned_at=record.pinned_at.isoformat() if record.pinned_at else None,
            gate_evidence_run_id=record.gate_evidence_run_id,
            note=record.note,
        )
        for record in records
    ]


@router.get(
    "/baselines/resolve",
    response_model=BaselineRow | None,
    summary="Spec §4.2 解析算法（main-latest 现算，不缓存）",
)
def resolve_baseline(
    workspace: WorkspaceDep,
    benchmark: Annotated[str, Query()],
    mode: Annotated[str, Query(description="explicit | release | main-latest")] = "main-latest",
    dataset_version: Annotated[str | None, Query()] = None,
) -> BaselineRow | None:
    from agent_eval.models.regression import BaselineMode

    try:
        resolved_mode = BaselineMode(mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unknown baseline mode: {mode}") from exc
    if resolved_mode == BaselineMode.no_baseline:
        raise HTTPException(status_code=400, detail="NO_BASELINE 是降级状态而非可选模式")
    baseline = workspace.baseline_store().resolve(benchmark, dataset_version, resolved_mode)
    if baseline is None:
        return None
    return BaselineRow(
        id=baseline.id,
        benchmark_id=baseline.benchmark_id,
        dataset_version=baseline.dataset_version,
        mode=baseline.mode.value,
        pinned_run_id=baseline.pinned_run_id,
        pinned_by=baseline.pinned_by,
        pinned_at=baseline.pinned_at.isoformat() if baseline.pinned_at else None,
        gate_evidence_run_id=baseline.gate_evidence_run_id,
        note=baseline.note,
    )
