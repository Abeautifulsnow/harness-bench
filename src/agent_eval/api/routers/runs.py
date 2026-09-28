"""Runs / Traces / Artifacts 只读接口（PRD §75/§76/§81/§82/§83）。

三个数据来源优先级：已落盘产物（report.json / gate.json）→ 事实层（run.json +
case_runs/*.json）→ 派生层（DuckDB）。因此就算投影层没 rebuild，Run Detail 也能看。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from agent_eval.api.deps import WorkspaceDep
from agent_eval.api.schemas import (
    ArtifactContent,
    ArtifactRow,
    CaseArtifactContent,
    CaseArtifactRow,
    CaseArtifacts,
    CaseResultRow,
    RunOverview,
    SpanNode,
    TraceEventRow,
    TraceEvents,
    TraceView,
)
from agent_eval.api.services import (
    CONTENT_TYPES,
    MAX_PREVIEW_BYTES,
    TEXT_ARTIFACTS,
    TEXTUAL_CASE_TYPES,
    RunView,
    artifact_file_within,
    case_content_type,
    load_view,
)
from agent_eval.errors import AgentEvalError
from agent_eval.models.artifacts import UNAVAILABLE_KINDS
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.storage.run_store import RunStore, _safe
from agent_eval.trace.builder import TraceBuilder

router = APIRouter(tags=["runs"])


def _view(workspace: WorkspaceDep, run_id: str) -> RunView:
    try:
        return load_view(workspace.run_store(), run_id, workspace.evals_root)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.get("/runs", response_model=list[RunMetadata], summary="PRD §81 runs 列表")
def list_runs(
    workspace: WorkspaceDep,
    benchmark: Annotated[str | None, Query()] = None,
    dataset_version: Annotated[str | None, Query()] = None,
    experiment_id: Annotated[str | None, Query()] = None,
    status: Annotated[
        str | None, Query(description="queued | running | completed | partial")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[RunMetadata]:
    metas = workspace.run_store().list_runs()
    if benchmark:
        metas = [m for m in metas if m.benchmark_id == benchmark]
    if dataset_version:
        metas = [m for m in metas if m.dataset_version == dataset_version]
    if experiment_id:
        metas = [m for m in metas if m.experiment_id == experiment_id]
    if status:
        metas = [m for m in metas if m.status.value == status]
    metas.sort(key=lambda m: m.started_at, reverse=True)
    return metas[:limit]


@router.get("/runs/{run_id}", response_model=RunOverview, summary="PRD §75 Overview tab")
def run_overview(run_id: str, workspace: WorkspaceDep) -> RunOverview:
    view = _view(workspace, run_id)
    aggregate = view.aggregate()
    return RunOverview(
        run=view.meta,
        counts=aggregate.counts,
        metrics=aggregate.metrics,
        verdict=aggregate.verdict,
        warnings=aggregate.warnings,
        cost=aggregate.cost.as_dict(),
        baseline_mode=aggregate.baseline_mode,
    )


@router.get("/runs/{run_id}/cases", response_model=list[CaseResultRow], summary="§75 Cases tab")
def run_cases(run_id: str, workspace: WorkspaceDep) -> list[CaseResultRow]:
    view = _view(workspace, run_id)
    rows: list[CaseResultRow] = []
    for case in view.aggregate().cases:
        payload = case.as_dict()
        rows.append(
            CaseResultRow(
                case_id=payload["case_id"],
                case_run_ids=[
                    result.id for result in view.results if result.case_id == payload["case_id"]
                ],
                iterations=payload["iterations"],
                pass_rate=payload["pass_rate"],
                stability=payload["stability"],
                regression_state=payload["regression_state"],
                valid_iterations=payload["valid_iterations"],
                infra_error_count=payload["infra_error_count"],
                score_mean=payload["score_mean"],
                score_stddev=payload["score_stddev"],
                tool_calls_mean=payload["tool_calls_mean"],
                tokens_mean=payload["tokens_mean"],
                latency_mean=payload["latency_mean"],
                cost_mean=payload["cost_mean"],
                pass_at_k=payload["pass_at_k"],
                blocking_failures=payload["blocking_failures"],
                error_semantics=payload["failure_semantics"],
                failure_category=payload["failure_category"],
                tags=payload["tags"],
            )
        )
    return rows


@router.get(
    "/runs/{run_id}/cases/{case_id}/metrics",
    response_model=list[dict],
    summary="PRD §83 metric_results（含 turn 级挂载点）",
)
def case_metrics(run_id: str, case_id: str, workspace: WorkspaceDep) -> list[dict[str, Any]]:
    view = _view(workspace, run_id)
    out: list[dict[str, Any]] = []
    hits = [r for r in view.results if r.case_id == case_id]
    if not hits:
        raise HTTPException(status_code=404, detail=f"case not in run: {case_id}")
    for result in sorted(hits, key=lambda r: r.iteration):
        for metric in result.all_metric_results:
            out.append(
                {
                    "case_run_id": result.id,
                    "case_id": result.case_id,
                    "iteration": result.iteration,
                    "metric": metric.metric,
                    "evaluator": metric.evaluator,
                    "score": metric.score,
                    "threshold": metric.threshold,
                    "verdict": metric.verdict,
                    "blocking": metric.blocking,
                    "mount": metric.metadata.get("mount"),
                    "turn": metric.metadata.get("turn"),
                    "reason": metric.reason,
                    "hard_gate": bool(metric.metadata.get("hard_gate")),
                }
            )
    return out


@router.get("/runs/{run_id}/cases/{case_id}/runs", response_model=list[dict], summary="§82 CaseRun")
def case_run_rows(run_id: str, case_id: str, workspace: WorkspaceDep) -> list[dict[str, Any]]:
    view = _view(workspace, run_id)
    hits = [r for r in view.results if r.case_id == case_id]
    if not hits:
        raise HTTPException(status_code=404, detail=f"case not in run: {case_id}")
    return [
        {
            "case_run_id": result.id,
            "iteration": result.iteration,
            "status": result.status.value,
            "failure_semantics": (
                result.failure_semantics.value if result.failure_semantics else None
            ),
            "failure_category": result.failure_category,
            "latency_ms": result.latency_ms,
            "token_count": result.token_count,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_tokens": result.cache_tokens,
            "cost": result.cost if result.cost_known else None,
            "cost_known": result.cost_known,
            "tool_calls": [call.name for call in result.tool_calls],
            "final_output": result.final_output,
            "error": result.error,
            "trace_available": bool(result.trace_path),
            "started_at": result.started_at,
            "finished_at": result.finished_at,
        }
        for result in sorted(hits, key=lambda r: r.iteration)
    ]


# ------------------------------------------------------------------- traces


def _trace_view(store: RunStore, run_id: str, case_id: str, iteration: int) -> TraceView:
    events = store.load_events(run_id, f"{_safe(case_id)}.iter{iteration}")
    if not events:
        return TraceView(run_id=run_id, case_id=case_id, iteration=iteration)
    builder = TraceBuilder()
    builder.feed_all(events)
    tree = builder.build()
    case_metrics = _metric_index(store, run_id, case_id, iteration)
    nodes = {span.id: _span_node(span, case_metrics) for span in tree.spans}
    root: SpanNode | None = None
    for span in tree.spans:
        node = nodes[span.id]
        if span.parent_span_id and span.parent_span_id in nodes:
            nodes[span.parent_span_id].children.append(node)
        elif root is None:
            root = node
    return TraceView(
        run_id=run_id,
        case_id=case_id,
        iteration=iteration,
        trace_id=tree.trace_id,
        span_count=len(tree.spans),
        tokens=tree.token_count(),
        root=root,
        tool_sequence=tree.tool_sequence(),
    )


def _metric_index(
    store: RunStore, run_id: str, case_id: str, iteration: int
) -> list[dict[str, Any]]:
    """该 iteration 的全部判定（PRD §83）。UI 把 case 级判定挂在根节点上。"""
    _meta, results = store.load_run(run_id)
    out: list[dict[str, Any]] = []
    for result in results:
        if result.case_id != case_id or result.iteration != iteration:
            continue
        for metric in result.all_metric_results:
            out.append(
                {
                    "metric": metric.metric,
                    "evaluator": metric.evaluator,
                    "verdict": metric.verdict,
                    "score": metric.score,
                    "threshold": metric.threshold,
                    "blocking": metric.blocking,
                    "mount": metric.metadata.get("mount"),
                    "turn": metric.metadata.get("turn"),
                    "reason": metric.reason,
                }
            )
    return out


def _span_node(span: Any, case_metrics: list[dict[str, Any]]) -> SpanNode:
    usage = (span.attributes or {}).get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    duration_ms: float | None = None
    if span.finished_at and span.started_at:
        duration_ms = round((span.finished_at - span.started_at).total_seconds() * 1000, 3)
    return SpanNode(
        id=span.id,
        parent_span_id=span.parent_span_id,
        type=span.type,
        name=span.name,
        started_at=span.started_at.isoformat() if span.started_at else None,
        finished_at=span.finished_at.isoformat() if span.finished_at else None,
        duration_ms=duration_ms,
        status=span.status,
        error=span.error,
        tokens=tokens or None,
        input=span.input,
        output=span.output,
        # 判定是 case/iteration 级的（Spec §2.2 三个挂载点），不属于单个 span：
        # 只在根节点上呈现，避免 UI 把同一条判定重复显示在多个节点里。
        metric_results=case_metrics if span.parent_span_id is None else [],
        attributes=span.attributes or {},
    )


@router.get(
    "/runs/{run_id}/traces/{case_id}",
    response_model=TraceView,
    summary="PRD §76 Trace Viewer（Span Tree）",
)
def trace_view(
    run_id: str,
    case_id: str,
    workspace: WorkspaceDep,
    iteration: Annotated[int, Query(ge=1)] = 1,
) -> TraceView:
    return _trace_view(workspace.run_store(), run_id, case_id, iteration)


@router.get("/traces", response_model=list[dict], summary="PRD §84 /api/traces：可回放 trace 索引")
def list_traces(
    workspace: WorkspaceDep,
    run_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[dict[str, Any]]:
    """列出已落盘的 raw trace（事实层文件，不依赖 DuckDB 投影）。

    一次 run 里 trace 是逐 ``(case, iteration)`` 存的文件；这里把它摊平成索引，
    Trace Viewer 可以直接从索引进 detail，不必先猜 case/iteration 组合。
    """
    rows: list[dict[str, Any]] = []
    store = workspace.run_store()
    metas = store.list_runs()
    metas.sort(key=lambda meta: meta.started_at, reverse=True)
    for meta in metas:
        if run_id and meta.run_id != run_id:
            continue
        traces_dir = store.run_dir_for(meta.run_id) / "traces"
        if not traces_dir.is_dir():
            continue
        for path in sorted(traces_dir.glob("*.events.jsonl")):
            key = path.name.removesuffix(".events.jsonl")
            case_id, _, iteration = key.rpartition(".iter")
            rows.append(
                {
                    "run_id": meta.run_id,
                    "benchmark_id": meta.benchmark_id,
                    "case_id": case_id,
                    "iteration": int(iteration) if iteration.isdigit() else 1,
                    "bytes": path.stat().st_size,
                    "url": f"/api/runs/{meta.run_id}/traces/{case_id}?iteration={iteration or 1}",
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


@router.get(
    "/runs/{run_id}/traces/{case_id}/events",
    response_model=TraceEvents,
    summary="PRD §52 Raw Trace（事件流回放）",
)
def trace_events(
    run_id: str,
    case_id: str,
    workspace: WorkspaceDep,
    iteration: Annotated[int, Query(ge=1)] = 1,
) -> TraceEvents:
    events = workspace.run_store().load_events(run_id, f"{_safe(case_id)}.iter{iteration}")
    return TraceEvents(
        projection="ok" if events else "missing",
        hint=None if events else "该 iteration 没有保存 trace（run 时未开启 --save-trace）",
        run_id=run_id,
        case_id=case_id,
        iteration=iteration,
        trace_id=events[0].trace_id if events else None,
        count=len(events),
        events=[
            TraceEventRow(
                event_id=event.event_id,
                type=event.type,
                timestamp=event.timestamp.isoformat(),
                parent_span_id=event.parent_span_id,
                data=event.data,
            )
            for event in events
        ],
    )


# ---------------------------------------------------------------- artifacts


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactRow], summary="§6.2 产物清单")
def list_artifacts(run_id: str, workspace: WorkspaceDep) -> list[ArtifactRow]:
    view = _view(workspace, run_id)
    return [ArtifactRow(**row) for row in view.artifacts()]


@router.get(
    "/runs/{run_id}/artifacts/{name}",
    response_model=ArtifactContent,
    summary="PRD §75 Artifacts tab（文本类产物直读；HTML 走 /raw）",
)
def artifact_content(run_id: str, name: str, workspace: WorkspaceDep) -> ArtifactContent:
    if name not in TEXT_ARTIFACTS:
        raise HTTPException(
            status_code=400,
            detail=f"artifact not readable as text: {name}（可用 /raw 端点获取原文件）",
        )
    view = _view(workspace, run_id)
    path = view.store.report_path(run_id, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"artifact not found: {name}")
    return ArtifactContent(
        name=name,
        content_type=CONTENT_TYPES.get(name, "text/plain"),
        text=path.read_text(encoding="utf-8"),
    )


@router.get(
    "/runs/{run_id}/artifacts/{name}/raw",
    response_class=PlainTextResponse,
    summary="产物原文（report.html / junit.xml 可直接在浏览器查看）",
)
def artifact_raw(run_id: str, name: str, workspace: WorkspaceDep) -> PlainTextResponse:
    if name not in CONTENT_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown artifact: {name}")
    view = _view(workspace, run_id)
    path = view.store.report_path(run_id, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"artifact not found: {name}")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type=CONTENT_TYPES[name])


# ------------------------------------------------------ case-level artifacts


@router.get(
    "/runs/{run_id}/cases/{case_id}/artifacts",
    response_model=CaseArtifacts,
    summary="PRD §90 case 级产物索引（含采集能力表）",
)
def case_artifacts(run_id: str, case_id: str, workspace: WorkspaceDep) -> CaseArtifacts:
    """一次 case 的现场清单：产物索引 + 采集记账 + 能力表。

    把 ``unavailable`` 一起返回是有意的（PRD §57 的可见性要求）：UI 需要能区分
    "本次没有截图采集能力"与"截图采集失败了"，前者是能力缺口，后者要报账。
    """
    view = _view(workspace, run_id)
    if not any(result.case_id == case_id for result in view.results):
        raise HTTPException(status_code=404, detail=f"case not in run: {case_id}")
    return CaseArtifacts(
        run_id=run_id,
        case_id=case_id,
        items=[CaseArtifactRow(**row) for row in view.case_artifact_rows(case_id)],
        notes=view.case_artifact_notes(case_id),
        unavailable=dict(UNAVAILABLE_KINDS),
    )


def _case_artifact_file(
    workspace: WorkspaceDep, run_id: str, case_id: str, iteration: int, name: str
) -> tuple[RunView, Path]:
    """索引查名 → 落到真实文件。查不到 / 越界一律 404，不做"猜路径"。

    路径穿越的防线只在索引这一层：请求里的 name 必须与某条已验证过的索引项**全等**
    才会被解析（Spec §21.3）。这样 ``../../etc/passwd``、``C:\\Windows\\win.ini``
    之类的输入连进入文件系统的机会都没有——它们在索引里不存在。
    """
    view = _view(workspace, run_id)
    hit = view.find_case_artifact(case_id, iteration, name)
    if hit is None:
        raise HTTPException(
            status_code=404,
            detail=f"artifact not found for case {case_id} iter{iteration}: {name}",
        )
    _result, record = hit
    path = artifact_file_within(view.store, run_id, record.path)
    if path is None:
        # 索引里有、磁盘上没有（或被删/被替换成越界指向）：仍然是"取不到"。
        raise HTTPException(status_code=404, detail=f"artifact file missing on disk: {name}")
    return view, path


@router.get(
    "/runs/{run_id}/cases/{case_id}/artifact-raw/{name:path}",
    response_class=FileResponse,
    summary="case 级产物原文（二进制也照原样下载）",
)
def case_artifact_raw(
    run_id: str,
    case_id: str,
    name: str,
    workspace: WorkspaceDep,
    iteration: Annotated[int, Query(ge=1)] = 1,
) -> FileResponse:
    _view_, path = _case_artifact_file(workspace, run_id, case_id, iteration, name)
    return FileResponse(path, media_type=case_content_type(name), filename=Path(name).name)


@router.get(
    "/runs/{run_id}/cases/{case_id}/artifacts/{name:path}",
    response_model=CaseArtifactContent,
    summary="PRD §90 case 级产物内容（文本预览；二进制走 artifact-raw）",
)
def case_artifact_content(
    run_id: str,
    case_id: str,
    name: str,
    workspace: WorkspaceDep,
    iteration: Annotated[int, Query(ge=1)] = 1,
) -> CaseArtifactContent:
    _view_, path = _case_artifact_file(workspace, run_id, case_id, iteration, name)
    content_type = case_content_type(name)
    if content_type not in TEXTUAL_CASE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"artifact is not text-previewable: {content_type}"
                "（请用 artifact-raw 端点下载原文件）"
            ),
        )
    try:
        # 只读上限 +1 字节：预览是"按需读"。trace 这类产物不受 fixture 的内容
        # 上限约束，整文件 read_bytes() 会把它全部拉进内存；bytes 报真实大小，
        # 让"截断了多少"可算。
        size = path.stat().st_size
        with path.open("rb") as fh:
            raw = fh.read(MAX_PREVIEW_BYTES + 1)
    except OSError:
        # 索引判定与真实读取之间文件可能消失：仍按"取不到"回 404，不是 500。
        raise HTTPException(
            status_code=404, detail=f"artifact file missing on disk: {name}"
        ) from None
    truncated = len(raw) > MAX_PREVIEW_BYTES
    return CaseArtifactContent(
        name=name,
        content_type=content_type,
        bytes=size,
        truncated=truncated,
        text=raw[:MAX_PREVIEW_BYTES].decode("utf-8", errors="replace"),
    )


@router.get("/runs/{run_id}/status", response_model=dict, summary="Run 状态（轮询用）")
def run_status(run_id: str, workspace: WorkspaceDep) -> dict[str, Any]:
    view = _view(workspace, run_id)
    return {
        "run_id": run_id,
        "status": view.meta.status.value,
        "verdict": view.aggregate().verdict if view.meta.status != RunStatus.running else None,
        "finished_at": view.meta.finished_at.isoformat() if view.meta.finished_at else None,
    }
