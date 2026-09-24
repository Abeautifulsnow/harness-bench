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


@pytest.fixture(autouse=True)
def deepeval_absent(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """默认让 judge 能力探针报告"不可用"（Spec §7.3 的 probe 结果）。

    用例断言的是 native 评测路径与 §7.4 fallback 记账。这些结论**不应**取决于开发机
    上有没有装 deepeval：装了但没有模型凭据时，judge 调用失败会让 run 正确地降级为
    exit 2（评估不可靠，Spec §6.1）——那是另一条语义路径，由专门用例覆盖，
    不应把整批 native 用例一起染红。

    需要真实 judge 行为的用例标 ``@pytest.mark.real_judge`` 退出该夹具。
    """
    if request.node.get_closest_marker("real_judge"):
        return
    from agent_eval.evaluators import deepeval_adapter as adapter_mod

    monkeypatch.setattr(adapter_mod.DeepEvalCapabilityAdapter, "probe", lambda self: {})
