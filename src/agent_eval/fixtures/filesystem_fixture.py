"""FilesystemFixture：把 fixtures/<name>/ 拷贝进 iteration 工作目录（PRD §89）。"""

from __future__ import annotations

import shutil
from pathlib import Path

from agent_eval.fixtures.base import FixtureHandle, FixtureProvider, resolve_fixture_dir
from agent_eval.models.case import EnvironmentSpec


class FilesystemFixture(FixtureProvider):
    name = "filesystem"

    def __init__(self, fixtures_root: Path) -> None:
        self._root = fixtures_root

    async def prepare(self, spec: EnvironmentSpec, workdir: Path) -> FixtureHandle:
        if not spec.fixture:
            handle_dir = workdir / "workspace"
            handle_dir.mkdir(parents=True, exist_ok=True)
            return FixtureHandle(name="none", workdir=handle_dir)
        source = resolve_fixture_dir(self._root, spec.fixture)
        target = workdir / "workspace"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
        return FixtureHandle(name=spec.fixture, workdir=target)

    async def cleanup(self, handle: FixtureHandle) -> None:
        shutil.rmtree(handle.workdir, ignore_errors=True)
