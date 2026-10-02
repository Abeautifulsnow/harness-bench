"""定义层只读接口：benchmarks / datasets / cases / suites（PRD §73, §11–§21）。

定义层是事实：直接读 ``evals/`` 文件树，不经过 DuckDB，因此投影未构建时目录类
页面依然可用（UI 不会整体变空）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from agent_eval.api.deps import WorkspaceDep
from agent_eval.api.schemas import BenchmarkRow, CaseRow, ProfileRow, SuiteRow
from agent_eval.errors import AgentEvalError
from agent_eval.loading.loader import (
    dataset_refs,
    load_all_datasets,
    load_benchmark,
    load_dataset,
    load_profile,
    load_suites,
    resolve_cases,
    select_suite_cases,
)
from agent_eval.models.benchmark import BenchmarkDef, DatasetInfo, SuiteDef
from agent_eval.models.case import Case
from agent_eval.models.run import RunMetadata

router = APIRouter(tags=["catalog"])

CaseQuery = Annotated[str | None, Query(description="dataset 引用：<id> 或 <id>@<version>")]


def _load_all_cases(root: Path, ref: str) -> tuple[DatasetInfo, list[Case]]:
    try:
        return load_dataset(root, ref)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


# ---------------------------------------------------------------- benchmarks


@router.get("/benchmarks", response_model=list[BenchmarkRow], summary="PRD §73 Benchmark UI")
def list_benchmarks(workspace: WorkspaceDep) -> list[BenchmarkRow]:
    root = workspace.evals_root
    benchmarks_dir = root / "benchmarks"
    rows: list[BenchmarkRow] = []
    latest: dict[str, RunMetadata] = {}
    for meta in workspace.run_store().list_runs():
        current = latest.get(meta.benchmark_id)
        if current is None or meta.started_at > current.started_at:
            latest[meta.benchmark_id] = meta
    if not benchmarks_dir.is_dir():
        return rows
    for path in sorted(benchmarks_dir.glob("*.yaml")):
        name = path.stem
        try:
            definition = load_benchmark(root, name)
        except AgentEvalError:
            continue
        last = latest.get(definition.name)
        rows.append(
            _benchmark_row(
                root,
                definition,
                last,
                _read_report(workspace.data_root, last.run_id) if last else None,
            )
        )
    return rows


def _benchmark_row(
    root: Path, definition: BenchmarkDef, last: RunMetadata | None, report: dict[str, Any] | None
) -> BenchmarkRow:
    row = BenchmarkRow(
        name=definition.name,
        description=definition.description,
        owner=definition.owner,
        dataset=definition.dataset,
        suites=list(definition.suites),
    )
    try:
        info, cases = load_dataset(root, definition.dataset)
        row.version = info.version
        # 计数口径必须与 `/benchmarks/{name}/cases` 一致：**套件选中**的条数，
        # 不是 dataset 里的条数。两者不等的案例真实存在（database-core 是
        # 40 vs 24：challenge 与 security 套件不在它的 suites 里，那 16 条 case
        # 因此永远不会被这个 benchmark 执行）。"目录里数得到、run 时跑不到"正是
        # 最该被看见的静默漏跑；在列表页把它算成已覆盖，等于把缺口藏起来。
        row.cases = len(resolve_cases(definition, load_suites(root), cases))
    except AgentEvalError:
        pass
    if last is not None:
        row.last_run_id = last.run_id
        row.last_run_at = last.started_at.isoformat()
        row.baseline_mode = last.baseline_mode
    if report is not None:
        totals = report.get("totals") or {}
        row.verdict = report.get("verdict")
        row.regressions = int(totals.get("regression_cases") or 0)
        iterations = totals.get("iterations") or 0
        if iterations:
            row.pass_rate = round(int(totals.get("passed_iterations") or 0) / iterations, 6)
    return row


def _read_report(data_root: Path, run_id: str) -> dict[str, Any] | None:
    """读已落盘的 report.json（缺失/损坏时返回 None，由调用方降级）。"""
    path = data_root / "runs" / run_id / "report.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


@router.get("/benchmarks/{name}", response_model=BenchmarkDef, summary="Benchmark 定义")
def get_benchmark(name: str, workspace: WorkspaceDep) -> BenchmarkDef:
    try:
        return load_benchmark(workspace.evals_root, name)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.get(
    "/benchmarks/{name}/cases",
    response_model=list[CaseRow],
    summary="PRD §21 Case Set 解析结果（suite → case）",
)
def benchmark_cases(name: str, workspace: WorkspaceDep) -> list[CaseRow]:
    root = workspace.evals_root
    try:
        definition = load_benchmark(root, name)
        suites = load_suites(root)
        info, cases = load_dataset(root, definition.dataset)
        selected = resolve_cases(definition, suites, cases)
    except AgentEvalError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return [_case_row(case, info.id) for case in selected]


# ---------------------------------------------------------------- datasets


@router.get("/datasets", response_model=list[DatasetInfo], summary="PRD §13 Dataset 列表")
def list_datasets(workspace: WorkspaceDep) -> list[DatasetInfo]:
    out: list[DatasetInfo] = []
    for ref in dataset_refs(workspace.evals_root):
        info, _ = _load_all_cases(workspace.evals_root, ref)
        out.append(info)
    return out


@router.get("/datasets/{dataset_id}", response_model=DatasetInfo, summary="PRD §13 Dataset 详情")
def get_dataset(dataset_id: str, workspace: WorkspaceDep) -> DatasetInfo:
    info, _ = _load_all_cases(workspace.evals_root, dataset_id)
    return info


# ---------------------------------------------------------------- cases


@router.get("/cases", response_model=list[CaseRow], summary="PRD §14 Case 目录")
def list_cases(
    workspace: WorkspaceDep,
    dataset: CaseQuery = None,
    tag: Annotated[list[str] | None, Query(description="重复传参即 AND 过滤")] = None,
    q: Annotated[str | None, Query(description="id/name/description 子串匹配")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[CaseRow]:
    refs = [dataset] if dataset else dataset_refs(workspace.evals_root)
    rows: list[CaseRow] = []
    for ref in refs:
        if not ref:
            continue
        info, cases = _load_all_cases(workspace.evals_root, ref)
        for case in cases:
            if tag and not all(t in case.tags for t in tag):
                continue
            if q:
                needle = q.lower()
                haystack = " ".join(filter(None, (case.id, case.name)))
                if needle not in haystack.lower():
                    continue
            rows.append(_case_row(case, info.id))
    return rows[:limit]


@router.get("/cases/{case_id}", response_model=Case, summary="PRD §14 Case 定义（含断言）")
def get_case(
    case_id: str,
    workspace: WorkspaceDep,
    dataset: CaseQuery = None,
) -> Case:
    refs = [dataset] if dataset else dataset_refs(workspace.evals_root)
    for ref in refs:
        if not ref:
            continue
        _, cases = _load_all_cases(workspace.evals_root, ref)
        for case in cases:
            if case.id == case_id:
                return case
    raise HTTPException(status_code=404, detail=f"case not found: {case_id}")


def _mount_points(case: Case) -> list[str]:
    """Case 声明的判定挂载点（Spec §2.2），供 UI 显示"这条 case 到底在判什么"。"""
    mounts: list[str] = []
    expected = case.expected
    if expected.output and not expected.output.is_empty():
        mounts.append("output")
    if expected.tools.required or expected.tools.forbidden:
        mounts.append("tools")
    if expected.constraints and expected.constraints.model_dump(exclude_none=True):
        mounts.append("constraints")
    if expected.security and expected.security.model_dump(exclude_defaults=True):
        mounts.append("security")
    if expected.extensions:
        mounts.append("extensions")
    if case.input.turns and any(turn.expect for turn in case.input.turns):
        mounts.append("turns")
    if case.expected_final is not None:
        mounts.append("final")
    return mounts


def _case_row(case: Case, dataset_id: str) -> CaseRow:
    return CaseRow(
        id=case.id,
        name=case.name,
        version=case.version,
        dataset_id=dataset_id,
        tags=list(case.tags),
        difficulty=case.difficulty,
        turns=len(case.input.turns) or 1,
        mount_points=_mount_points(case),
        description=(case.context[0] if case.context else ""),
    )


# ---------------------------------------------------------------- suites


@router.get("/suites", response_model=list[SuiteRow], summary="PRD §19/§62 Suite 与安全套件")
def list_suite_rows(workspace: WorkspaceDep) -> list[SuiteRow]:
    root = workspace.evals_root
    try:
        suites: dict[str, SuiteDef] = load_suites(root)
        # 计数必须用真实选择结果：套件通常只声明 tags（case_ids 为空），
        # 按 len(case_ids) 计会让每个套件都显示 0 个 case，与实际跑了几条矛盾。
        #
        # 定义树整树只装一次：suites/*.yaml 的计数与下方安全套件的 tag 计数读的是
        # 同一份 case，分头装载会让同一请求把每个 dataset 解析两遍（Spec §22.12）。
        cases_by_ref, errors = load_all_datasets(root)
        if errors:
            ref, exc = next(iter(errors.items()))
            raise HTTPException(status_code=400, detail=f"{ref}: {exc.message}")
        all_cases = [case for cases in cases_by_ref.values() for case in cases]
        counts = {name: len(select_suite_cases(suite, all_cases)) for name, suite in suites.items()}
    except AgentEvalError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    out = [
        SuiteRow(
            name=suite.name,
            tags=list(suite.tags),
            case_ids=list(suite.case_ids),
            cases=counts[suite.name],
        )
        for suite in suites.values()
    ]
    # 安全/红队套件按 tag 选择 case，不在 suites/*.yaml 里，单独成行
    from agent_eval.security.suites import list_suites

    for summary in list_suites(root, cases_by_ref=cases_by_ref):
        out.append(
            SuiteRow(
                name=summary.name,
                tags=[summary.tag],
                cases=summary.cases,
                kind="security" if summary.tag == "security" else "red-team",
                description=summary.description,
            )
        )
    return out


@router.get(
    "/profiles",
    response_model=list[ProfileRow],
    summary="Metric Profile 列表（Execution 文档 §12：Web 只选择，不修改）",
)
def list_profile_rows(workspace: WorkspaceDep) -> list[ProfileRow]:
    root = workspace.evals_root
    profiles_dir = root / "profiles"
    out: list[ProfileRow] = []
    if not profiles_dir.is_dir():
        return out
    for path in sorted(profiles_dir.glob("*.yaml")):
        try:
            profile = load_profile(root, path.stem)
        except AgentEvalError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        out.append(
            ProfileRow(
                name=profile.name,
                metrics=len(profile.metrics),
                blocking_metrics=sum(1 for m in profile.metrics if m.blocking),
                judge_concurrency=profile.judge_concurrency,
                strict_protocol=profile.strict_protocol,
            )
        )
    return out
