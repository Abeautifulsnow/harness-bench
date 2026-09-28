"""FixtureProvider 抽象与注册（PRD §89）。

生命周期（Spec §2.4）：每个 iteration prepare → 执行 → snapshot → cleanup，
iteration 间零共享。``snapshot`` 的位置是契约的一部分：它必须在 cleanup **之前**
被调用，否则 provider 已经销毁了要观测的东西（Spec §21.2）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_eval.errors import InvalidCallError
from agent_eval.models.artifacts import SnapshotArtifact
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

    async def snapshot(self, handle: FixtureHandle) -> list[SnapshotArtifact]:
        """采集 cleanup 前的环境现场（PRD §90，Spec §21）。

        缺省实现返回空列表——**不是**每个 provider 都有值得留存的状态
        （如"无 fixture"的纯 echo 用例）。这里用空列表而不是 ``None``：
        "未产出"与"产出了零项"在调用侧是同一件事，多一个 ``None`` 只会多出
        一条永远走不到的分支（与 Spec §17 第 8 条"未声明 ≠ 声明为空"相反——
        那里的两者含义不同，这里的两者含义相同）。

        约定：**不抛异常**。快照失败不应把一次已经跑完的执行变成 infra error，
        采集不到就少一件产物，并在 ``note`` 里说明（调用侧兜底记 note）。
        """
        return []

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
