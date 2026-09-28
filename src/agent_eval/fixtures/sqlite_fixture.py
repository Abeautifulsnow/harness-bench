"""SQLiteFixture：fixtures/<name>/seed.sql → iteration 独立临时库（PRD §89）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.fixtures.base import FixtureHandle, FixtureProvider, resolve_fixture_dir
from agent_eval.models.artifacts import ArtifactKind, SnapshotArtifact
from agent_eval.models.case import EnvironmentSpec

# 库快照的文本上限。超过就截断在最后一整条语句，并如实标注 truncated——
# .sql 是给人看的现场，不是用来恢复的备份（恢复靠 seed.sql + fixture 重建）。
MAX_DUMP_BYTES = 512 * 1024
SNAPSHOT_NAME = "database.sql"
TRUNCATION_NOTE = "dump 已截断（在最后一条完整语句处）"


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

    async def snapshot(self, handle: FixtureHandle) -> list[SnapshotArtifact]:
        """导出 SQL dump（PRD §90 的 database snapshot）。

        用 ``iterdump()`` 而不是复制 .db 文件：dump 是可读文本，能直接在报告与
        REST 里看、能 grep，二进制副本在同样信息量下多占空间且看不见内容。
        库被 agent 删了或损坏时返回空——快照失败不该把一次跑完的执行变成 error。
        """
        db_path = Path(handle.info.get("path") or "")
        if not db_path.is_file():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                dump = "\n".join(conn.iterdump())
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        raw = dump.encode("utf-8")
        if len(raw) <= MAX_DUMP_BYTES:
            return [
                SnapshotArtifact(
                    name=SNAPSHOT_NAME,
                    kind=ArtifactKind.database,
                    content=raw,
                    note="导出为 SQL dump（iterdump），非二进制副本",
                )
            ]
        # 截到最后一条完整语句：半个 INSERT 比截断本身更容易误导人。
        cut = raw[:MAX_DUMP_BYTES].rfind(b";\n")
        kept = raw[: cut + 2] if cut > 0 else raw[:MAX_DUMP_BYTES]
        return [
            SnapshotArtifact(
                name=SNAPSHOT_NAME,
                kind=ArtifactKind.database,
                content=kept + f"-- {TRUNCATION_NOTE}\n".encode(),
                truncated=True,
                note=TRUNCATION_NOTE,
            )
        ]

    async def cleanup(self, handle: FixtureHandle) -> None:
        db_path = Path(handle.info.get("path", ""))
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

