"""执行结束后的环境观测快照（Spec §19）。

扩展断言里有一类判定的对象不是"agent 说了什么"，而是"环境变成了什么样"：
``database_state``（表还剩几行）、``file_state``（文件还在不在）。它们的观测来源
是 fixture，且必须在 **cleanup 之前**采集——fixture 的清理会删掉库文件与工作目录。

采集与判定分离：Runner 只负责把 fixture 的 handle 变成一份快照，判定逻辑留在
native evaluator（Spec §2.2：断言词汇表的求值点只有一处）。快照自身不做判定，
观测不到就是 ``None``，由求值方决定判 skipped（Spec §19.1 的统一口径：
**观测不足不是 pass**）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from agent_eval.fixtures.base import FixtureHandle

# 物化进快照的表行数上限：快照要落盘进报告，整表复制会让报告膨胀到不可读。
# 超出上限时记为 ">= 上限"，判定只用到行数，不需要内容。
TABLE_SCAN_LIMIT = 10_000


@dataclass
class TableState:
    exists: bool
    rows: int | None = None  # exists=False 时为 None
    truncated: bool = False  # rows 达到扫描上限（"至少这么多行"）


@dataclass
class EnvironmentSnapshot:
    """一次 iteration 结束时的环境事实（Spec §19.2/§19.3）。"""

    db_path: Path | None = None
    workdir: Path | None = None
    _tables: dict[str, TableState] = field(default_factory=dict)

    # ------------------------------------------------------------ database

    def table(self, name: str) -> TableState | None:
        """返回表状态；无数据库可读时返回 None（观测不足，不是"表不存在"）。"""
        if self.db_path is None or not Path(self.db_path).is_file():
            return None
        if name not in self._tables:
            self._tables[name] = _read_table(Path(self.db_path), name)
        return self._tables[name]

    # ------------------------------------------------------------ file

    def file_text(self, relative_path: str) -> str | None:
        """读取 workdir 内文件；不存在或越界返回 None。

        越界（``../`` 逃出 workdir）与"文件不存在"都返回 None：判定方关心的是
        "能不能拿到内容"，而越界本身在启动期就已由 fixture 边界校验拦过一次，
        这里再抛错只会让一条断言的失败变成整轮 ERROR。
        """
        if self.workdir is None:
            return None
        target = _inside(self.workdir, relative_path)
        if target is None or not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def file_exists(self, relative_path: str) -> bool | None:
        if self.workdir is None:
            return None
        target = _inside(self.workdir, relative_path)
        return None if target is None else target.exists()


def _inside(root: Path, relative_path: str) -> Path | None:
    """把相对路径解析到 root 内；越界返回 None（Fail-fast 的软版本，见 file_text）。"""
    try:
        root_resolved = root.resolve()
        candidate = (root_resolved / relative_path).resolve()
    except OSError:
        return None
    if candidate != root_resolved and root_resolved not in candidate.parents:
        return None
    return candidate


def escapes_relative(path: str) -> bool:
    """路径声明是否越出 workdir（绝对路径，或含 ``..`` 分量）。

    与"文件不存在"必须分开：越界是**声明写错了**，判 error 让人去改用例；
    报成"文件不存在"会让人去查 agent，查错方向（Spec §19.3）。

    纯字符串判定，不依赖 workdir 是否已知：`case validate` 在启动期就要拦下它。
    刻意保守——连 `a/../b` 这种自我抵消的写法也拒掉：workdir 内的相对路径
    没有任何正当理由出现 `..`（Spec §19.3）。
    """
    if not path or Path(path).is_absolute():
        return True
    return ".." in Path(path).parts


def _read_table(db_path: Path, name: str) -> TableState:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if row is None:
            return TableState(exists=False)
        # 表名来自 case YAML：标识符无法参数化，只能按字符集校验（见 valid_table_name）。
        # 子查询里的 LIMIT 才真的限制扫描行数——`SELECT COUNT(*) ... LIMIT n`
        # 的 LIMIT 作用在结果行上（永远只有一行），限制不了扫描。
        quoted = '"' + name.replace('"', '""') + '"'
        count = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted} LIMIT ?)", (TABLE_SCAN_LIMIT,)
        ).fetchone()
        rows = int(count[0]) if count else 0
        return TableState(exists=True, rows=rows, truncated=rows >= TABLE_SCAN_LIMIT)
    finally:
        conn.close()


def snapshot_from_handle(handle: FixtureHandle) -> EnvironmentSnapshot:
    """从 fixture handle 采集快照（必须在 provider.cleanup 之前调用）。"""
    db_path = handle.info.get("path")
    return EnvironmentSnapshot(
        db_path=Path(db_path) if db_path else None,
        workdir=Path(handle.workdir) if handle.workdir else None,
    )


def valid_table_name(name: str) -> bool:
    """表名合法性（db 标识符无法参数化，只能白名单字符集）。"""
    return bool(name) and all(ch.isalnum() or ch in "_." for ch in name)
