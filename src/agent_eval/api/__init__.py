"""P5 只读 REST API（PRD §84）。

分层约定（与 Spec §1.3 一致）：

- **事实层**（``evals/`` 定义树、``<data_root>/runs/*``）→ 直接读文件，永远可用；
- **派生层**（DuckDB 投影、baselines / reviews / drafts 的 jsonl）→ 读投影，
  但**不写**：投影缺失时返回"未构建"提示，而不是隐式建空库。

因此本包不含任何 POST/PUT/DELETE 路由。Web UI 只能读，写操作留在 CLI，
避免出现"UI 绕过 Gate / Review 流程直接改事实数据"的路径（PRD §110-10）。
"""

from agent_eval.api.app import create_app

__all__ = ["create_app"]
