"""参数值的语义等价判定（PRD §57 semantic diff）。

入口只有一个：``compare_values``。trace diff 用它替代原先的 ``a != b``，
于是"语义相同、字面不同"不再被报成参数变化。

**每条归一化规则都必须显式、必须可测、必须能说不**（Spec §20.1 的规则表）。
回归平台里漏判（false negative）比误报危险得多：误报让人多点一次"确认"，
漏判让真实回归静默通过。所以默认态度是**宁可保留差异**——
只在"两个写法在语义上确实指向同一件事"时才归一化：

| 规则 | 判相同 | 反例（必须仍判不同） |
| --- | --- | --- |
| 数字 | `1` vs `1.0`、`"1"` vs `"1.0"` | `1` vs `"1"`（跨类型，见下） |
| 路径 | `./a/b.csv` vs `a/b.csv` vs `a/b.csv/` | `a/b` vs `a/c` |
| SQL | `SELECT 1` vs `select  1;` | `id = 1` vs `id like '%1%'` |

跨类型数字（`1` 与 `"1"`）**判不同**：PRD §57 把 `1` / `1.0` / `"1"` 列为一组，
但 int 与 str 的边界是工具参数里真实存在的一类回归（下游会把它们当不同的值），
判相同就会漏掉它。这是对 PRD 例子的一处**有意收窄**，理由记在 Spec §20.1。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from agent_eval.regression.sql_diff import compare_sql, looks_like_sql


class ComparisonKind(StrEnum):
    """两个值是按哪个层级判的（PRD §57 的 structural / semantic，加上 AST）。"""

    structural = "structural"
    semantic = "semantic"
    ast = "ast"


@dataclass(frozen=True)
class Equivalence:
    """一次值比对的结论。

    ``same`` 为真且 ``kind`` 非 structural 时值得上报为"语义相同"；
    ``degraded_kind`` 非空表示这次结论只在低置信层级成立（报告按分类去重展示）。
    """

    same: bool
    kind: ComparisonKind
    degraded: str | None = None
    degraded_kind: str | None = None
    detail: str | None = None


_NUMBERISH = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def numeric_value(value: object) -> Decimal | None:
    """把 int/float/数字串折成 Decimal；不是数字返回 None（bool 明确排除）。

    float 走 `str()` 而不是 Decimal(float)：``Decimal(0.1)`` 会把二进制误差
    原样带进来，``1.1`` 与 ``"1.1"`` 就会判成不同。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str) and _NUMBERISH.match(value.strip()):
        try:
            return Decimal(value.strip())
        except InvalidOperation:  # pragma: no cover - 正则已限定可解析形状
            return None
    return None


def normalize_path(text: str) -> str:
    """路径归一化：去掉前导 ``./``、折叠重复斜杠、去掉尾随 ``/``。

    只处理这三种写法——它们在"指向同一个文件"这一点上无歧义。
    不做 ``..`` 折叠：``a/../b`` 与 ``b`` 的等价性依赖 cwd 语义，
    而工具参数里的路径是给 agent 自己用的，折叠会掩盖"它到底请求了哪个文件"。
    """
    text = text.strip()
    while text.startswith("./"):
        text = text[2:]
    if "//" in text:
        text = re.sub(r"/{2,}", "/", text)
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def compare_values(
    baseline: object,
    candidate: object,
    *,
    sql_dialect: str | None = None,
) -> Equivalence:
    """判定两个参数值是否语义相同。

    比对阶梯（处处"宁可保留差异"）：

    1. 一侧是 bool 另一侧不是 → 不同。Python 里 ``True == 1``，JSON 里它们是
       两个值，不拦住会让 ``true`` / ``1`` 的参数变化静默消失；
    2. 同类型且相等 → 相同（structural：没有任何归一化介入）；
    3. 两侧都是字符串 → 走 SQL / 数字串 / 路径三条归一化规则；
    4. 两侧都是数字（int/float 混用）→ 数值比较（semantic）；
    5. 其余 → 按 Python 相等性判，不算相同就是不同。
    """
    if isinstance(baseline, bool) != isinstance(candidate, bool):
        return Equivalence(same=False, kind=ComparisonKind.structural)
    if type(baseline) is type(candidate) and baseline == candidate:
        return Equivalence(same=True, kind=ComparisonKind.structural)
    if isinstance(baseline, str) and isinstance(candidate, str):
        return _compare_strings(baseline, candidate, sql_dialect)
    base_num, cand_num = numeric_value(baseline), numeric_value(candidate)
    if (
        base_num is not None
        and cand_num is not None
        and _numeric_types_match(baseline, candidate)
        and base_num == cand_num
    ):
        return Equivalence(
            same=True,
            kind=ComparisonKind.semantic,
            detail=f"{baseline!r} 与 {candidate!r} 数值相同",
        )
    return Equivalence(same=baseline == candidate, kind=ComparisonKind.structural)


def _numeric_types_match(baseline: object, candidate: object) -> bool:
    """跨类型数字不判相同（Spec §20.1 的有意收窄）。

    ``1`` 与 ``"1"``：int/str 的边界是工具参数里真实存在的回归，判相同会漏掉它。
    同一侧是 bool 时也判不同（``True`` 与 ``1`` 是两种值）。
    """
    if isinstance(baseline, bool) or isinstance(candidate, bool):
        return False
    return isinstance(baseline, int | float) == isinstance(candidate, int | float)


def _compare_strings(baseline: str, candidate: str, sql_dialect: str | None) -> Equivalence:
    if looks_like_sql(baseline) and looks_like_sql(candidate):
        result = compare_sql(baseline, candidate, sql_dialect)
        kind = ComparisonKind.ast if result.level == "ast" else ComparisonKind.semantic
        detail = None
        if result.same and result.normalized:
            detail = f"归一化后均为 {result.normalized[0]!r}"
        return Equivalence(
            same=result.same,
            kind=kind,
            degraded=result.degraded,
            degraded_kind=result.degraded_kind,
            detail=detail,
        )

    base_num, cand_num = numeric_value(baseline), numeric_value(candidate)
    if base_num is not None and cand_num is not None and base_num == cand_num:
        return Equivalence(
            same=True,
            kind=ComparisonKind.semantic,
            detail=f"{baseline!r} 与 {candidate!r} 数值相同",
        )

    if "/" in baseline and "/" in candidate:
        base_path, cand_path = normalize_path(baseline), normalize_path(candidate)
        if base_path == cand_path:
            return Equivalence(
                same=True,
                kind=ComparisonKind.semantic,
                detail=f"路径归一化后均为 {base_path!r}",
            )
    return Equivalence(same=False, kind=ComparisonKind.structural)
