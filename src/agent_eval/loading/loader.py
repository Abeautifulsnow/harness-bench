"""YAML loaders for evals/ definition tree (PRD §11–§21, Spec §2.3/§7.1).

Layout under the evals root (default ``evals/``)::

    datasets/<id>/dataset.yaml      {id, version, description}
    datasets/<id>/cases/*.yaml      Case files (Spec §2.3 schema)
    suites/<name>.yaml              {name, tags[], case_ids[]}
    benchmarks/<name>.yaml          {name, dataset, suites[], default_profile}
    profiles/<name>.yaml            {name, metrics[], judge_concurrency}
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import IO, Any

from agent_eval.errors import AgentEvalError, InvalidCallError
from agent_eval.models.benchmark import BenchmarkDef, DatasetInfo, SuiteDef
from agent_eval.models.case import Case
from agent_eval.models.profile import MetricProfile

# libyaml 的 C 解析器与 SafeLoader 语义相同（都是 safe 面），实测 40 条 case
# 从 40ms 降到 4ms。定义层每次请求都要整树解析，纯 Python 解析器是这条路径的主要
# 开销；libyaml 缺失时回退，只是慢，不是功能降级。
# Spec §22.12：这里只换解析器，行为不变。
try:  # pragma: no cover - 取决于本机 wheel 是否带 libyaml
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _SafeLoader


def _parse_yaml(fh: IO[str]) -> Any:
    """等价于 ``yaml.safe_load``，只把解析器类换成 libyaml 的 ``CSafeLoader``。

    语义与 ``safe_load`` 逐字相同（它的实现就是这个 loader 类的实例化 +
    ``get_single_data`` + ``dispose`` 三行），区别只在 C 解析器 vs 纯 Python：
    实测 40 条 case 从 40ms 降到 4ms。写成实例形式而非把类交给模块级入口，是要让
    真正的风险面（非 safe 解析器）与静态扫描的误报区分开——本文件的输入是仓库内的
    ``evals/`` 定义树，但仍坚持只走 safe 语义的解析器。
    """
    loader = _SafeLoader(fh)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InvalidCallError(f"definition file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = _parse_yaml(fh)
    if not isinstance(data, dict):
        raise InvalidCallError(f"definition file must contain a mapping: {path}")
    return data


def dataset_refs(root: Path) -> list[str]:
    """``datasets/`` 下所有有 ``dataset.yaml`` 的目录名（排序）——定义层的唯一枚举点。

    枚举分散在多个模块里时，"哪些 dataset 算存在"会各自漂移。
    """
    datasets_dir = root / "datasets"
    if not datasets_dir.is_dir():
        return []
    return sorted(d.name for d in datasets_dir.iterdir() if (d / "dataset.yaml").is_file())


def load_all_datasets(root: Path) -> tuple[dict[str, list[Case]], dict[str, AgentEvalError]]:
    """一次读完 ``datasets/`` 下每个 dataset，返回 ``{ref: cases}`` 与 ``{ref: error}``。

    单个 dataset 定义损坏时**返回错误而不是抛出**：调用方对它的策略不同——
    ``/api/suites`` 要据此报 400，而套件清单要跳过损坏的那个、把其余部分显示出来。
    用同一个装载结果服务两类调用方，策略留在调用方，定义层只负责"读一遍"。
    """
    cases_by_ref: dict[str, list[Case]] = {}
    errors: dict[str, AgentEvalError] = {}
    for ref in dataset_refs(root):
        try:
            _, cases = load_dataset(root, ref)
        except AgentEvalError as exc:
            errors[ref] = exc
            continue
        cases_by_ref[ref] = cases
    return cases_by_ref, errors


def load_dataset(root: Path, ref: str) -> tuple[DatasetInfo, list[Case]]:
    """Load ``<dataset_id>@<version>`` (or ``@latest`` → the single defined version)."""
    dataset_id, _, requested = ref.partition("@")
    requested = requested or "latest"
    base = root / "datasets" / dataset_id
    meta = _read_yaml(base / "dataset.yaml")

    version = str(meta.get("version", ""))
    if not version:
        raise InvalidCallError(f"dataset.yaml missing 'version': {base}")
    if requested != "latest" and requested != version:
        raise InvalidCallError(
            f"dataset version mismatch: requested {dataset_id}@{requested}, "
            f"defined {dataset_id}@{version}"
        )

    cases_dir = base / "cases"
    case_files = sorted(cases_dir.glob("*.yaml")) if cases_dir.is_dir() else []
    if not case_files:
        raise InvalidCallError(f"dataset {dataset_id}@{version} has no cases under {cases_dir}")

    digest = hashlib.sha256()
    digest.update((base / "dataset.yaml").read_bytes())
    cases: list[Case] = []
    for path in case_files:
        digest.update(path.read_bytes())
        raw = _read_yaml(path)
        try:
            cases.append(Case.model_validate(raw))
        except Exception as exc:  # pydantic ValidationError → invalid call
            raise InvalidCallError(f"invalid case file {path.name}: {exc}") from exc

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise InvalidCallError(f"duplicate case id '{case.id}' in dataset {dataset_id}")
        seen.add(case.id)

    info = DatasetInfo(
        id=dataset_id,
        version=version,
        description=str(meta.get("description", "")),
        hash=digest.hexdigest(),
    )
    return info, cases


def load_suites(root: Path) -> dict[str, SuiteDef]:
    suites: dict[str, SuiteDef] = {}
    suites_dir = root / "suites"
    if not suites_dir.is_dir():
        return suites
    for path in sorted(suites_dir.glob("*.yaml")):
        raw = _read_yaml(path)
        try:
            suite = SuiteDef.model_validate(raw)
        except Exception as exc:
            raise InvalidCallError(f"invalid suite file {path.name}: {exc}") from exc
        suites[suite.name] = suite
    return suites


def load_benchmark(root: Path, name: str) -> BenchmarkDef:
    raw = _read_yaml(root / "benchmarks" / f"{name}.yaml")
    try:
        return BenchmarkDef.model_validate(raw)
    except Exception as exc:
        raise InvalidCallError(f"invalid benchmark '{name}': {exc}") from exc


DEFAULT_PROFILE = MetricProfile(name="default", metrics=[])


def load_profile(root: Path, name: str) -> MetricProfile:
    if name == DEFAULT_PROFILE.name and not (root / "profiles" / f"{name}.yaml").is_file():
        return DEFAULT_PROFILE.model_copy()
    raw = _read_yaml(root / "profiles" / f"{name}.yaml")
    try:
        return MetricProfile.model_validate(raw)
    except Exception as exc:
        raise InvalidCallError(f"invalid profile '{name}': {exc}") from exc


def select_suite_cases(suite: SuiteDef, cases: list[Case]) -> dict[str, Case]:
    """一个 Suite 的选择结果：先按 tags 收，再补齐显式 case_ids（PRD §19/§21）。

    这是"套件到底选了哪些 case"的**唯一**实现：``resolve_suites`` 与只读接口
    （``GET /api/suites`` 的计数列）都走它，否则两处会各算一遍并且不一致。
    """
    by_id = {c.id: c for c in cases}
    picked: dict[str, Case] = {}
    for tag in suite.tags:
        for case in cases:
            if tag in case.tags:
                picked[case.id] = case
    for case_id in suite.case_ids:
        if case_id not in by_id:
            raise InvalidCallError(f"suite '{suite.name}' references unknown case '{case_id}'")
        picked[case_id] = by_id[case_id]
    return picked


def resolve_suites(
    benchmark: BenchmarkDef,
    suites: dict[str, SuiteDef],
    cases: list[Case],
    suite_filter: list[str] | None = None,
    tag_filter: list[str] | None = None,
) -> tuple[list[Case], dict[str, int]]:
    """Union of suite selections (tags first, then explicit ids), deduped, order-stable.

    Returns ``(cases, {suite_name: selected_case_count})``. The counts are the fact
    source for the Release Gate's required-suite rule (PRD §108): a suite whose cases
    were all filtered out counts 0 — that is "not covered", not "passed vacuously".

    ``suite_filter`` overrides the benchmark's default suite list, so a release run can
    name the four suites it must execute even when the benchmark declares fewer.
    """
    names = list(dict.fromkeys(suite_filter or benchmark.suites))
    for name in names:
        if name not in suites:
            defined = ", ".join(sorted(suites)) or "none"
            raise InvalidCallError(
                f"unknown suite '{name}' referenced by benchmark "
                f"'{benchmark.name}' (defined suites: {defined})"
            )

    selected: dict[str, Case] = {}
    counts: dict[str, int] = {}
    for suite_name in names:
        picked = select_suite_cases(suites[suite_name], cases)
        if tag_filter:
            picked = {cid: c for cid, c in picked.items() if any(t in c.tags for t in tag_filter)}
        counts[suite_name] = len(picked)
        selected.update(picked)

    if not selected:
        raise InvalidCallError(
            f"benchmark '{benchmark.name}' selects no cases"
            + (f" for tags {tag_filter}" if tag_filter else "")
            + (f" from suites {names}" if suite_filter else "")
        )
    return list(selected.values()), counts


def resolve_cases(
    benchmark: BenchmarkDef,
    suites: dict[str, SuiteDef],
    cases: list[Case],
    tag_filter: list[str] | None = None,
) -> list[Case]:
    """Case selection only (kept for callers that need no per-suite counts)."""
    return resolve_suites(benchmark, suites, cases, None, tag_filter)[0]
