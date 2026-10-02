"""Agent Connection Profile（设计文档 §8/§27/§28/§44 PR4）。

Web 不让用户填任意 URL：endpoint 由管理员以 Definition Plane 资产的形式
预注册在 ``evals/agents/*.yaml``（随 Git 版本管理，Web 只读）。凭证以
``secret_ref``（env var 名）进仓库，值只在本进程内解析，从不返回给浏览器。

SSRF 立场（§28）：V1 不开动态 endpoint 输入，注册表本身就是主防线；
scheme 校验在这里兜底，防的是手滑写错协议，不是防管理员。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, field_validator
from yaml import SafeLoader
from yaml import load as _yaml_load

from agent_eval.errors import InvalidCallError


class AgentAuth(BaseModel):
    type: Literal["bearer"]
    # env var 的**名字**，不是值。名字随 Git 走，值留在部署环境里。
    secret_ref: str


class AgentConnectionProfile(BaseModel):
    id: str
    display_name: str
    endpoint: str
    enabled: bool = True
    description: str = ""
    auth: AgentAuth | None = None

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        # fake:// 是一等公民（本地联调 / 测试）；其余只放行 http(s)。
        if value == "fake://":
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"agent endpoint must be fake:// or an http(s) URL: {value}")
        return value.rstrip("/")

    def secret_resolved(self) -> bool:
        if self.auth is None:
            return True
        return bool(os.environ.get(self.auth.secret_ref))

    def auth_headers(self) -> dict[str, str]:
        """解析凭证为请求头。声明了 auth 但 env 缺失时硬失败（§27 Invalid）。"""

        if self.auth is None:
            return {}
        token = os.environ.get(self.auth.secret_ref)
        if not token:
            raise InvalidCallError(
                f"agent connection '{self.id}': secret '{self.auth.secret_ref}' "
                "is not configured in the environment"
            )
        return {"Authorization": f"Bearer {token}"}


class AgentConnectionView(BaseModel):
    """API 出参：浏览器能看到的名字与状态，看不到任何凭证值（§27）。"""

    id: str
    display_name: str
    endpoint: str
    enabled: bool
    description: str = ""
    auth_type: Literal["bearer"] | None = None
    secret_ref: str | None = None
    secret_state: Literal["configured", "missing", "none"] = "none"

    @classmethod
    def of(cls, profile: AgentConnectionProfile) -> AgentConnectionView:
        if profile.auth is None:
            return cls(
                id=profile.id,
                display_name=profile.display_name,
                endpoint=profile.endpoint,
                enabled=profile.enabled,
                description=profile.description,
            )
        return cls(
            id=profile.id,
            display_name=profile.display_name,
            endpoint=profile.endpoint,
            enabled=profile.enabled,
            description=profile.description,
            auth_type=profile.auth.type,
            secret_ref=profile.auth.secret_ref,
            secret_state="configured" if profile.secret_resolved() else "missing",
        )


class AgentHealthReport(BaseModel):
    """/health 探测结果（§9）。detail 截断到 500 字符，防止异常 body 刷屏。"""

    ok: bool
    agent_model: str | None = None
    observation_surface: dict[str, bool] = Field(default_factory=dict)
    detail: str = ""


def load_agent_connections(evals_root: Path) -> list[AgentConnectionProfile]:
    """装载 ``evals/agents/*.yaml``。目录不存在 = 还没注册任何 agent（空表）。

    单个文件坏了必须炸出来（InvalidCallError），不能静默跳过——静默消失的
    注册项会让"选它发评测"变成玄学故障。
    """

    directory = evals_root / "agents"
    if not directory.is_dir():
        return []
    profiles: list[AgentConnectionProfile] = []
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            raw = _yaml_load(fh, SafeLoader)
        try:
            profiles.append(AgentConnectionProfile.model_validate(raw))
        except Exception as exc:
            raise InvalidCallError(f"invalid agent connection '{path.name}': {exc}") from exc
    return profiles


async def check_agent_health(profile: AgentConnectionProfile) -> AgentHealthReport:
    if profile.endpoint == "fake://":
        # 内置 mock：进程内即可达，观测面以 FakeAgentAdapter 的声明为准——
        # 这里只回答"能不能跑"，不做假探测。
        return AgentHealthReport(ok=True, agent_model="fake", detail="in-process fake adapter")

    headers = profile.auth_headers()
    try:
        async with httpx.AsyncClient(
            base_url=profile.endpoint, headers=headers, timeout=10.0
        ) as client:
            resp = await client.get("/health")
    except (httpx.HTTPError, OSError) as exc:
        return AgentHealthReport(ok=False, detail=f"health unreachable: {exc}")

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:300]}
    detail = str(body) if resp.is_success else f"HTTP {resp.status_code}: {body}"
    ok = resp.is_success and isinstance(body, dict) and body.get("status") == "ok"
    return AgentHealthReport(
        ok=ok,
        agent_model=str(body.get("agent_model")) if body.get("agent_model") else None,
        observation_surface=(
            {str(k): bool(v) for k, v in body["observation_surface"].items()}
            if isinstance(body, dict) and isinstance(body.get("observation_surface"), dict)
            else {}
        ),
        detail=detail[:500],
    )
