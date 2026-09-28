"""SQL 语义比对（PRD §57）：归一化 + AST diff + 显式降级。

PRD §57 要求 Tool Arguments 的 diff 分两级（structural / semantic），并建议
"SQL 使用 SQLGlot 增加 AST Diff"。本模块是 semantic 级里 SQL 这一支的实现：

```text
文本归一化（无依赖）   大小写、空白、尾随分号
AST 归一化（sqlglot）  标识符引号、等价语法
```

**为什么分两级而不是只做 AST**：sqlglot 是可选依赖（pyproject 的 `sql` extra）。
报告里的 diff 结论会进 Gate，不能因为某台机器没装 sqlglot 就给出不同结论——
所以文本归一化必须自己扛住"大小写 + 空白"这类最常见的等价，
AST 只用来吃掉它吃不掉的部分（如 ``count( * )`` 与 ``COUNT(*)``）。

**降级必须可见**：parse 失败（非法 SQL）或 sqlglot 缺失时，结论带上
``degraded`` 说明，绝不在"没能力判断"时静默判"相同"或"不同"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

try:  # 可选依赖：缺失时降级到文本归一化，并让降级原因可见
    from sqlglot import exp as _exp
    from sqlglot import parse_one as _parse_one
    from sqlglot.errors import SqlglotError as _SqlglotError
except ImportError:  # pragma: no cover - 取决于安装形态
    _exp = None
    _parse_one = None

    class _SqlglotError(Exception):  # type: ignore[no-redef]
        """占位：sqlglot 缺失时 parse 分支走不到，但异常类型要能引用。"""


# 平台 environment.database 声明 → sqlglot 方言名（PRD §89 的 provider 词汇）。
# 方言来自 case 的 environment 声明，不硬编码：换 fixture provider 时这条映射
# 是唯一要跟着改的地方，漏改会以"parse 失败 → 降级"的形式显形，不会静默判错。
_DIALECTS: dict[str, str] = {
    "sqlite": "sqlite",
    "postgres": "postgres",
    "postgresql": "postgres",
    "duckdb": "duckdb",
    "mysql": "mysql",
}

# 看起来像 SQL 的判据：只认语句开头，不做"包含 SELECT"这种松判据。
# 参数值里出现 "SELECT" 的字符串太多（自然语言、模板片段），
# 把不像 SQL 的东西送进解析器只会平白产生降级标注。
_SQL_HEAD = re.compile(
    r"^\s*(select|insert|update|delete|with|create|drop|alter|replace|pragma|explain)\b",
    re.IGNORECASE,
)

MISSING_DEP_HINT = "sqlglot 未安装（`uv sync --extra sql`），AST 层不可用"

# 降级原因的分类键（Spec §20.2）。报告侧按**分类**去重展示，按值的详细原因
# 留在 ``degraded`` 里给人复核——几十个参数各带一行同样的说明是噪声，
# 但每一处的具体报错又不能丢。
KIND_PARSE_FAILED = "sql_parse_failed"
KIND_MISSING_DEP = "sqlglot_missing"
KIND_DIALECT_DEFAULTED = "dialect_defaulted"

DEGRADATION_SUMMARIES: dict[str, str] = {
    KIND_PARSE_FAILED: ("部分 SQL 解析失败，这些参数只能按文本归一化判定（结论偏保守：保留差异）"),
    KIND_MISSING_DEP: MISSING_DEP_HINT + "，SQL 只能按文本归一化判定",
    KIND_DIALECT_DEFAULTED: "case 未声明 environment.database，SQL 按 sqlite 解析",
}


@dataclass(frozen=True)
class SqlComparison:
    """两侧 SQL 的比对结论。"""

    same: bool
    # "semantic" = 文本归一化后相同（或降级后在文本层判定）；"ast" = 走完 AST 层
    level: str
    # 降级详情（含具体的 parse 报错），供人复核；None = 无降级
    degraded: str | None = None
    # 降级分类（KIND_*），供报告去重；None = 无降级
    degraded_kind: str | None = None
    # 归一化后的文本（报告里展示"为什么判相同"）
    normalized: tuple[str, str] | None = None


def ast_available() -> bool:
    """AST 层是否可用：把"装了没装"变成一个可测的显式事实，而不是散落的 try。"""
    return _parse_one is not None


def dialect_for(database: str | None) -> str | None:
    """case 的 ``environment.database`` → sqlglot 方言；未知 provider 返回 None。"""
    if not database:
        return None
    return _DIALECTS.get(database.strip().lower())


def looks_like_sql(value: object) -> bool:
    return isinstance(value, str) and _SQL_HEAD.match(value) is not None


def normalize_sql(text: str) -> str:
    """文本归一化（Spec §20.1 的规则表）：关键字大小写、空白、尾随分号。

    两条必须守住的边界：

    1. **字符串字面量原样保留**。无脑 ``.lower()`` 会把 ``'ACME'`` 与 ``'acme'``
       判成同一个值——那是数据，不是关键字。这里逐字符扫描，只在引号外做折叠；
    2. **不动标点周围空白**。``id = 1`` 与 ``id=1`` 的等价性交给 AST：
       在文本层删空白会制造语法陷阱（``a - -b`` 去掉空格就成了注释 ``--``）。
       代价是"标点空白差异"依赖 sqlglot 才能判相同，这件事由降级标注说明。

    刻意的**不做**清单：不改标识符引号（``"t"`` 与 ``t`` 在关键字场景下含义不同）、
    不动字面量、不重排子句。只做语义上无歧义的归一化——"宁可保留差异"的具体形态。
    """
    out: list[str] = []
    quote: str | None = None
    pending_space = False
    i = 0
    while i < len(text):
        char = text[i]
        if quote is not None:
            out.append(char)
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:  # '' 转义，不算闭合
                    out.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in "'\"":
            quote = char
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(char)
            i += 1
            continue
        if char.isspace():
            pending_space = True
            i += 1
            continue
        if pending_space and out:
            out.append(" ")
            pending_space = False
        out.append(char.lower())
        i += 1
    return "".join(out).strip().rstrip(";").strip()


def compare_sql(baseline: str, candidate: str, dialect: str | None) -> SqlComparison:
    """SQL 比对：先文本归一化，再按需上 AST。"""
    base_norm, cand_norm = normalize_sql(baseline), normalize_sql(candidate)
    if base_norm == cand_norm:
        return SqlComparison(same=True, level="semantic", normalized=(base_norm, cand_norm))
    return _compare_ast(baseline, candidate, dialect, (base_norm, cand_norm))


def _compare_ast(
    baseline: str,
    candidate: str,
    dialect: str | None,
    normalized: tuple[str, str],
) -> SqlComparison:
    """AST 层比对。方言未声明时按 sqlite 解析，并如实标注这一点。"""
    if _parse_one is None:
        return SqlComparison(
            same=False,
            level="semantic",
            degraded=MISSING_DEP_HINT,
            degraded_kind=KIND_MISSING_DEP,
            normalized=normalized,
        )
    effective = dialect or "sqlite"
    note_kind = None if dialect else KIND_DIALECT_DEFAULTED
    try:
        base_ast = _canonical_ast(baseline, effective)
        cand_ast = _canonical_ast(candidate, effective)
    except _SqlglotError as exc:
        return SqlComparison(
            same=False,
            level="semantic",
            degraded=f"SQL 解析失败（dialect={effective}）：{exc}",
            degraded_kind=KIND_PARSE_FAILED,
            normalized=normalized,
        )
    same = base_ast == cand_ast or base_ast.sql(dialect=effective) == cand_ast.sql(
        dialect=effective
    )
    return SqlComparison(
        same=same,
        level="ast",
        degraded=DEGRADATION_SUMMARIES[note_kind] if note_kind else None,
        degraded_kind=note_kind,
        normalized=normalized,
    )


def _canonical_ast(sql: str, dialect: str) -> Any:
    """解析并清掉标识符引号：``"orders"`` / ``orders`` / ``[orders]`` 视为同一标识符。

    只清 ``Identifier.quoted``，不动字符串字面量——``'abc'`` 与 ``"abc"`` 在
    SQLite 里一个是字符串、另一个可能是标识符，那是有语义区别的。
    """
    tree = _parse_one(sql, dialect=dialect)
    for identifier in tree.find_all(_exp.Identifier):
        identifier.set("quoted", False)
    return tree
