"""CLI 子命令实现（PRD §70/§71；每个命令族一个模块）。

统一的调用约定：
  - 只捕获 ``AgentEvalError``（exit code 由异常类携带，Spec §6.1）
  - 查询类命令找不到对象 → exit 3（无效调用），不使用 exit 1
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

EVALS_ROOT = Path("evals")
FIXTURES_ROOT = Path("fixtures")
DATA_ROOT = Path(".agent-eval")

console = Console()

__all__ = ["EVALS_ROOT", "FIXTURES_ROOT", "DATA_ROOT", "console"]
