"""AgentAdapter 抽象（PRD §6.2）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from agent_eval.models.events import TraceEvent


class HealthStatus(BaseModel):
    """health 探测结果（PRD §7.1）。

    外部接入（change-plan A1 修订四/A2/A4）的三个 run 级事实都在这里一次上报，
    因为它们必须整份一次拿到、且在 per-case 执行前到位——``create_session`` 是
    per-case 的，各报一次会让"没声明"与"会话失败没报"混在一起：

    - ``observation_surface``：观测面能力表（**事件名 → bool**），E2 能力声明的
      载体。键不在表里 = 未声明 = 按"具备"处理；显式 False = 该观测面不存在，
      依赖它的插件判 skipped。键空间与 ``RunMetadata.metric_capability_snapshot``
      里 probe() 填的 metric id 不同，落库时加 ``event:`` 前缀区分。
    - ``agent_model``：SUT 自报的**实际生效**模型标识（A4）。比 CLI ``--model``
      标签权威——它是被测方自己承认在跑的模型；两者并存时以此为准。
    """

    ok: bool
    detail: str = ""
    observation_surface: dict[str, bool] = Field(default_factory=dict)
    agent_model: str | None = None


class SessionContext(BaseModel):
    """Eval 侧传给 Agent 的会话元数据（PRD §7.2）。"""

    eval_run_id: str
    case_id: str
    variant_id: str | None = None
    iteration: int = 1
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentSession(BaseModel):
    session_id: str
    # 注意：这是 create_session **请求载荷**的回显（adapter 自己拼的 metadata），
    # 不是服务端响应体——SUT 写不进来，别把它当回执通道用（change-plan A1 修订四）。
    metadata: dict[str, Any] = Field(default_factory=dict)
    # A1 修订四：workdir 可达性回执（create_session 响应侧新字段）。
    # True/False = SUT 明确回执；None = 未回执（未知——按 warning 记账，不得默认视为可达）。
    # False = SUT 声明不可达/不可写 → InfraError（配置错 ≠ agent 失败，exit 2）。
    workdir_accessible: bool | None = None


class AgentRequest(BaseModel):
    message: str
    stream: bool = True


class AgentFailureError(Exception):
    """Agent 通过事件流上报的错误（error 事件）→ AGENT_FAILURE（PRD §46）。"""


class AgentAdapter(ABC):
    @abstractmethod
    async def health_check(self) -> HealthStatus: ...

    @abstractmethod
    async def create_session(self, context: SessionContext) -> AgentSession: ...

    @abstractmethod
    def run(self, session: AgentSession, request: AgentRequest) -> AsyncIterator[TraceEvent]:
        """Stream agent events for one turn (1 turn = 1 run call, PRD §7.3)."""
        ...

    @abstractmethod
    async def cancel(self, session: AgentSession) -> None: ...
