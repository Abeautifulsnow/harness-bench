"""FixtureProvider 抽象与注册（PRD §89）。

生命周期（Spec §2.4）：每个 iteration prepare → 执行 → cleanup，iteration 间零共享。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_eval.errors import InvalidCallError
from agent_eval.models.case import EnvironmentSpec


@dataclass
class FixtureHandle:
    name: str
    workdir: Path
    info: dict[str, Any] = field(default_factory=dict)


class FixtureProvider(ABC):
    name: str = ""

    @abstractmethod
    async def prepare(self, spec: EnvironmentSpec, workdir: Path) -> FixtureHandle: ...

    @abstractmethod
    async def cleanup(self, handle: FixtureHandle) -> None: ...


def resolve_fixture_dir(fixtures_root: Path, name: str) -> Path:
    """把 fixture 名解析为 fixtures_root 下的目录，拒绝越界路径。

    dataset YAML 里的 `environment.fixture` 是仓库内数据，但一个 `../..` 就能让
    fixture 读取仓库外内容；这里统一做边界校验（Fail-fast，exit 3）。
    """
    root = fixtures_root.resolve()
    candidate = (root / name).resolve()
    if candidate != root and root not in candidate.parents:
        raise InvalidCallError(
            f"fixture '{name}' escapes the fixtures root {root} (Spec §89 boundary)"
        )
    if not candidate.is_dir():
        raise InvalidCallError(f"fixture not found: {candidate}")
    return candidate


def get_provider(spec: EnvironmentSpec, fixtures_root: Path) -> FixtureProvider:
    """Resolve provider from case environment.database (PRD §89 first batch)."""
    database = (spec.database or "filesystem").lower()
    if database == "sqlite":
        from agent_eval.fixtures.sqlite_fixture import SQLiteFixture

        return SQLiteFixture(fixtures_root)
    if database in {"postgres", "postgresql"}:
        raise InvalidCallError(
            "PostgresFixture is planned but not implemented in P0 "
            "(PRD §89); use sqlite or filesystem fixtures"
        )
    if database == "filesystem":
        from agent_eval.fixtures.filesystem_fixture import FilesystemFixture

        return FilesystemFixture(fixtures_root)
    raise InvalidCallError(f"unknown environment.database provider: {database}")
