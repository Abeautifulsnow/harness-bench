"""安全套件清单（PRD §62/§63/§108）。

安全套件的 case 通过 ``tag`` 选择：``security``（安全回归）与 ``red-team``（红队）。
套件本身是 evals 定义树的一部分，本模块只做"套件是否存在/覆盖多少 case"的检查，
不硬编码 case 列表。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_eval.cli.commands import EVALS_ROOT  # noqa: F401  (导出给命令层复用)

SUITE_TAGS = ("security", "red-team")


@dataclass
class SuiteSummary:
    name: str
    tag: str
    cases: int
    description: str


def list_suites(root: Path, dataset_ref: str | None = None) -> list[SuiteSummary]:
    """统计 datasets 下带安全标签的 case 数（不绑定具体 dataset）。"""
    from agent_eval.loading.loader import load_dataset

    counts = dict.fromkeys(SUITE_TAGS, 0)
    datasets_dir = root / "datasets"
    refs: list[str] = []
    if datasets_dir.is_dir():
        for dataset_dir in sorted(datasets_dir.iterdir()):
            if (dataset_dir / "dataset.yaml").is_file():
                refs.append(
                    dataset_ref
                    if dataset_ref and dataset_ref.startswith(f"{dataset_dir.name}@")
                    else dataset_dir.name
                )
    for ref in refs:
        try:
            _, cases = load_dataset(root, ref)
        except Exception:  # noqa: BLE001 — 单个 dataset 定义损坏不影响清单
            continue
        for case in cases:
            for tag in SUITE_TAGS:
                if tag in case.tags:
                    counts[tag] += 1
    descriptions = {
        "security": "安全回归断言（PRD §63 七类确定性规则）",
        "red-team": "红队攻击面覆盖（PRD §62 八类）",
    }
    return [SuiteSummary(tag, tag, counts[tag], descriptions[tag]) for tag in SUITE_TAGS]
