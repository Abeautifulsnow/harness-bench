"""Webhook / CI Trigger（V2，docs §52）。

触发器是 Definition Plane 资产（``evals/triggers/*.yaml``）：一份**可被外部系统
（CI / 人工脚本）凭 token 调用**的命名评测请求。token 值存环境变量，仓库里只有
``token_secret_ref``（env var 名）——与 Agent Connection 的 secret 约定一致（§27）。
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from pydantic import BaseModel
from yaml import SafeLoader
from yaml import load as _yaml_load

from agent_eval.errors import InvalidCallError
from agent_eval.execution.presets import PresetRequest


class NotifyDef(BaseModel):
    """任务终态 webhook 通知（V2 Notification 的最小形态）。"""

    webhook: str
    # 通知体的 Bearer 凭证（env var 名，值不进仓库）
    token_secret_ref: str | None = None


class TriggerDef(BaseModel):
    id: str
    display_name: str
    enabled: bool = True
    description: str = ""
    # 触发后提交的评测参数（preset 引用 + 局部覆盖均可）
    preset: str | None = None
    request: PresetRequest | None = None
    # Bearer token 的 env var 名；未声明 = 该触发器不校验（本地工具场景）
    token_secret_ref: str | None = None

    def verify_token(self, supplied: str | None) -> bool:
        if self.token_secret_ref is None:
            return True
        expected = os.environ.get(self.token_secret_ref)
        if not expected:
            # 声明了 ref 但环境没配：fail-closed，绝不放行
            return False
        return hmac.compare_digest(supplied or "", expected)

    def token_configured(self) -> bool:
        return self.token_secret_ref is None or bool(os.environ.get(self.token_secret_ref or ""))


class TriggerView(BaseModel):
    """API 出参：绝不回 token 值（§27）。"""

    id: str
    display_name: str
    enabled: bool
    description: str = ""
    preset: str | None = None
    token_ref: str | None = None
    token_state: str = "none"


def load_triggers(evals_root: Path) -> list[TriggerDef]:
    directory = evals_root / "triggers"
    if not directory.is_dir():
        return []
    triggers: list[TriggerDef] = []
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            raw = _yaml_load(fh, SafeLoader)
        try:
            triggers.append(TriggerDef.model_validate(raw))
        except Exception as exc:
            raise InvalidCallError(f"invalid trigger '{path.name}': {exc}") from exc
    return triggers
