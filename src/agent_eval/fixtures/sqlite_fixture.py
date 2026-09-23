"""SQLiteFixture：fixtures/<name>/seed.sql → iteration 独立临时库（PRD §89）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.fixtures.base import FixtureHandle, FixtureProvider, resolve_fixture_dir
from agent_eval.models.case import EnvironmentSpec


class SQLiteFixture(FixtureProvider):
    name = "sqlite"

    def __init__(self, fixtures_root: Path) -> None:
        self._root = fixtures_root

    async def prepare(self, spec: EnvironmentSpec, workdir: Path) -> FixtureHandle:
        if not spec.fixture:
            raise InvalidCallError("sqlite environment requires environment.fixture")
        seed_dir = resolve_fixture_dir(self._root, spec.fixture)
        seed_sql = seed_dir / "seed.sql"
        if not seed_sql.is_file():
            raise InvalidCallError(f"sqlite fixture seed not found: {seed_sql}")
        workdir.mkdir(parents=True, exist_ok=True)
        db_path = workdir / "fixture.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(seed_sql.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()
        return FixtureHandle(
            name=spec.fixture,
            workdir=workdir,
            info={"database": "sqlite", "path": str(db_path)},
        )

    async def cleanup(self, handle: FixtureHandle) -> None:
        db_path = Path(handle.info.get("path", ""))
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()
