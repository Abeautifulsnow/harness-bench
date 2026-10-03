"""Run Preset / Evaluation Template（V2，docs §52）。

预设是 Definition Plane 资产（``evals/presets/*.yaml``，随 Git 管理）：一份命名的
评测参数模板。字段允许**部分给出**——Web New Evaluation 用它预填表单，
Trigger/Schedule 用它发起评测（此时必填字段缺失会硬失败，绝不编造默认值）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from yaml import SafeLoader
from yaml import load as _yaml_load

from agent_eval.errors import InvalidCallError


class PresetRequest(BaseModel):
    """预设载荷：全部可选（模板 ≠ 完整请求）。"""

    benchmark: str | None = None
    agent_profile: str | None = None
    suite: list[str] | None = None
    profile: str | None = None
    repeat: int | None = None
    agent_concurrency: int | None = None
    no_judge: bool | None = None
    strict_protocol: bool | None = None
    save_trace: bool | None = None
    tags: list[str] | None = None
    baseline_policy: str | None = None
    baseline_run: str | None = None


class EvalPreset(BaseModel):
    id: str
    display_name: str
    description: str = ""
    request: PresetRequest = PresetRequest()


def load_presets(evals_root: Path) -> list[EvalPreset]:
    """装载 ``evals/presets/*.yaml``；坏文件炸出来（与 agent connections 同策略）。"""

    directory = evals_root / "presets"
    if not directory.is_dir():
        return []
    presets: list[EvalPreset] = []
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            raw = _yaml_load(fh, SafeLoader)
        try:
            presets.append(EvalPreset.model_validate(raw))
        except Exception as exc:
            raise InvalidCallError(f"invalid eval preset '{path.name}': {exc}") from exc
    return presets


def resolve_request(
    partial: PresetRequest,
    overrides: PresetRequest | None = None,
) -> dict:
    """预设 + 覆盖 → EvalRunRequest 的合法 kwargs。

    只覆盖显式给出的字段；返回值直接喂 ``EvalRunRequest.model_validate``——
    必填字段（benchmark / agent_profile）缺失时由 Pydantic 报错，调用方转 400。
    """

    merged = partial.model_dump(exclude_none=True)
    if overrides is not None:
        merged.update(overrides.model_dump(exclude_none=True))
    return merged
