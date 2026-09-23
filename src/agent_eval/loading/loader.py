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
from typing import Any

import yaml

from agent_eval.errors import InvalidCallError
from agent_eval.models.benchmark import BenchmarkDef, DatasetInfo, SuiteDef
from agent_eval.models.case import Case
from agent_eval.models.profile import MetricProfile


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InvalidCallError(f"definition file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise InvalidCallError(f"definition file must contain a mapping: {path}")
    return data


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


def resolve_cases(
    benchmark: BenchmarkDef,
    suites: dict[str, SuiteDef],
    cases: list[Case],
    tag_filter: list[str] | None = None,
) -> list[Case]:
    """Union of suite selections (tags first, then explicit ids), deduped, order-stable."""
    by_id = {c.id: c for c in cases}
    selected: dict[str, Case] = {}
    for suite_name in benchmark.suites:
        suite = suites.get(suite_name)
        if suite is None:
            raise InvalidCallError(
                f"benchmark '{benchmark.name}' references unknown suite '{suite_name}'"
            )
        for tag in suite.tags:
            for case in cases:
                if tag in case.tags:
                    selected[case.id] = case
        for case_id in suite.case_ids:
            if case_id not in by_id:
                raise InvalidCallError(f"suite '{suite_name}' references unknown case '{case_id}'")
            selected[case_id] = by_id[case_id]

    if tag_filter:
        selected = {cid: c for cid, c in selected.items() if any(t in c.tags for t in tag_filter)}
    if not selected:
        raise InvalidCallError(
            f"benchmark '{benchmark.name}' selects no cases"
            + (f" for tags {tag_filter}" if tag_filter else "")
        )
    return list(selected.values())
