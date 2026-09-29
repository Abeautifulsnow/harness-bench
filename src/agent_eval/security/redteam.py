"""Red Team 覆盖矩阵（PRD §62）。

八类攻击面各自需要至少一个 case；本模块负责把 evals 里的红队 case 归入攻击面，
并在有缺失时报出来（覆盖缺口是安全工作的可见负债，不能静默）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.models.case import Case

RED_TEAM_CATEGORIES = (
    "prompt_injection",
    "tool_injection",
    "permission_escalation",
    "data_exfiltration",
    "secret_access",
    "dangerous_commands",
    "unsafe_db_write",
    "malicious_mcp",
)

# 攻击面 → case id 里的识别片段
_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "prompt_injection": ("prompt-inject", "prompt_injection", "injection"),
    "tool_injection": ("tool-inject", "tool_injection"),
    "permission_escalation": ("permission", "escalation", "override"),
    "data_exfiltration": ("exfil", "data_leak", "exfiltration"),
    "secret_access": ("secret", "credential", "key_access"),
    "dangerous_commands": ("dangerous", "command", "shell"),
    "unsafe_db_write": ("unsafe-write", "unsafe_write", "drop", "delete"),
    "malicious_mcp": ("malicious", "mcp"),
}


@dataclass
class RedTeamCase:
    category: str
    case_id: str


def classify_red_team_case(case: Case) -> str | None:
    """把红队 case 归入攻击面：先看 ``red-team:<category>`` 标签，再退回 id 关键词。"""
    for tag in case.tags:
        if tag.startswith("red-team:") or tag.startswith("red_team:"):
            category = tag.split(":", 1)[1].strip().lower().replace("-", "_")
            if category in RED_TEAM_CATEGORIES:
                return category
    haystack = f"{case.id} {case.name}".lower()
    for category, hints in _CATEGORY_HINTS.items():
        if any(hint in haystack for hint in hints):
            return category
    return None


def classify_cases(cases: Iterable[Case]) -> list[RedTeamCase]:
    """把一批已装好的 case 归类为红队攻击面（选择口径的唯一实现）。

    ``load_red_team_cases`` 与只读接口（``GET /security`` 的覆盖矩阵）共用它：
    后者已经为同一请求读过定义树，再自己筛一遍会让选择口径出现第二份实现。
    """
    out: list[RedTeamCase] = []
    for case in cases:
        if "red-team" not in case.tags and "redteam" not in case.tags:
            continue
        category = classify_red_team_case(case)
        if category is not None:
            out.append(RedTeamCase(category=category, case_id=case.id))
    return out


def load_red_team_cases(root: Path) -> list[RedTeamCase]:
    """扫描 evals/datasets/* 中带 red-team 标签的 case。"""
    from agent_eval.loading.loader import load_dataset

    out: list[RedTeamCase] = []
    datasets_dir = root / "datasets"
    if not datasets_dir.is_dir():
        return out
    for dataset_dir in sorted(datasets_dir.iterdir()):
        if not (dataset_dir / "dataset.yaml").is_file():
            continue
        try:
            _, cases = load_dataset(root, dataset_dir.name)
        except InvalidCallError:
            continue
        out.extend(classify_cases(cases))
    return out


def coverage(root: Path) -> dict[str, list[str]]:
    """攻击面 → 覆盖它的 case id 列表（空列表 = 未覆盖）。"""
    result: dict[str, list[str]] = {category: [] for category in RED_TEAM_CATEGORIES}
    for case in load_red_team_cases(root):
        result[case.category].append(case.case_id)
    return result
