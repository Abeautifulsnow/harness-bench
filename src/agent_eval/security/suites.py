"""安全套件清单（PRD §62/§63/§108）。

安全套件的 case 通过 ``tag`` 选择：``security``（安全回归）与 ``red-team``（红队）。
套件本身是 evals 定义树的一部分，本模块只做"套件是否存在/覆盖多少 case"的检查，
不硬编码 case 列表。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_eval.cli.commands import EVALS_ROOT  # noqa: F401  (导出给命令层复用)
from agent_eval.models.case import Case

SUITE_TAGS = ("security", "red-team")

_DESCRIPTIONS = {
    "security": "安全回归断言（PRD §63 七类确定性规则）",
    "red-team": "红队攻击面覆盖（PRD §62 八类）",
}


@dataclass
class SuiteSummary:
    name: str
    tag: str
    cases: int
    description: str


def count_suite_cases(cases_by_ref: dict[str, list[Case]]) -> dict[str, int]:
    """按 tag 统计各安全套件覆盖的 case 数（PRD §62/§63）。

    与 ``list_suites`` 拆开，是为了让调用方能**复用已经装好的 dataset**：
    ``GET /api/suites`` 既列 suites/*.yaml 又列安全套件，同一份定义树装两遍
    是纯浪费（Spec §22.12）。
    """
    counts = dict.fromkeys(SUITE_TAGS, 0)
    for cases in cases_by_ref.values():
        for case in cases:
            for tag in SUITE_TAGS:
                if tag in case.tags:
                    counts[tag] += 1
    return counts


def list_suites(
    root: Path,
    dataset_ref: str | None = None,
    cases_by_ref: dict[str, list[Case]] | None = None,
) -> list[SuiteSummary]:
    """统计带安全标签的 case 数（不绑定具体 dataset）。

    ``cases_by_ref`` 已给出时不再自行装载——调用方（只读接口）在同一次请求里已经
    读过一遍定义树，重复读只是慢，结果一模一样。``dataset_ref`` 是钉住某个 dataset
    版本的入口，它绕开复用（版本钉住没法从"按目录名装载"的结果里还原）。
    """
    if dataset_ref is not None:
        # 版本钉住（`<id>@<version>`）只能按 ref 逐个装载还原，没法从"按目录名"
        # 的结果里恢复——这条路径上不复用，次数正确优先于次数少。
        return _list_suites_by_ref(root, dataset_ref)
    if cases_by_ref is None:
        from agent_eval.loading.loader import load_all_datasets

        cases_by_ref, _errors = load_all_datasets(root)
    counts = count_suite_cases(cases_by_ref)
    return [SuiteSummary(tag, tag, counts[tag], _DESCRIPTIONS[tag]) for tag in SUITE_TAGS]


def _list_suites_by_ref(root: Path, dataset_ref: str) -> list[SuiteSummary]:
    """``dataset_ref`` 点名了某个 dataset 时按该 ref 装载（其余仍按目录名）。"""
    from agent_eval.errors import AgentEvalError
    from agent_eval.loading.loader import dataset_refs, load_dataset

    counts = dict.fromkeys(SUITE_TAGS, 0)
    for ref in dataset_refs(root):
        target = dataset_ref if dataset_ref.startswith(f"{ref}@") else ref
        try:
            _, cases = load_dataset(root, target)
        except AgentEvalError:  # 单个 dataset 定义损坏不影响清单
            continue
        for case in cases:
            for tag in SUITE_TAGS:
                if tag in case.tags:
                    counts[tag] += 1
    return [SuiteSummary(tag, tag, counts[tag], _DESCRIPTIONS[tag]) for tag in SUITE_TAGS]
