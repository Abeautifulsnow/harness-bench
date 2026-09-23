"""共享测试夹具：临时 evals 树 + RunConfig 工厂。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def evals_tree(tmp_path: Path) -> tuple[Path, Path]:
    """拷贝仓库示例 evals/ + fixtures/ 到临时目录，返回 (evals_root, data_root)。"""
    evals_root = tmp_path / "evals"
    shutil.copytree(REPO / "evals", evals_root)
    fixtures_root = tmp_path / "fixtures"
    shutil.copytree(REPO / "fixtures", fixtures_root)
    data_root = tmp_path / "data"
    data_root.mkdir()
    return evals_root, data_root


@pytest.fixture()
def fixtures_root(tmp_path: Path) -> Path:
    root = tmp_path / "fixtures_standalone"
    shutil.copytree(REPO / "fixtures", root)
    return root
