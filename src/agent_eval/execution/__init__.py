"""Execution Plane（docs/web-evaluation-control-plane-design.md §6/§21）。

Web / CLI / 未来的 scheduler 共用的受控执行入口：Job 生命周期、队列与全局并发、
真实取消、Agent Connection Profile。唯一的评测执行核心仍是 ``runner.Runner``（§20）——
本包只编排"何时跑、能不能跑、跑到哪了"，绝不重新实现评测语义。
"""

from __future__ import annotations

from agent_eval.execution.connections import (
    AgentAuth,
    AgentConnectionProfile,
    AgentConnectionView,
    AgentHealthReport,
    check_agent_health,
    load_agent_connections,
)
from agent_eval.execution.cron import CronSpec
from agent_eval.execution.executor import LocalJobExecutor
from agent_eval.execution.models import (
    TERMINAL_STATUSES,
    EvalRunJob,
    EvalRunRequest,
    FailureKind,
    JobProgress,
    JobStatus,
)
from agent_eval.execution.presets import EvalPreset, PresetRequest, load_presets
from agent_eval.execution.repository import JobRepository
from agent_eval.execution.scheduler import ScheduleDef, SchedulerService, load_schedules
from agent_eval.execution.service import (
    DuplicateSubmission,
    EvalRunService,
    InvalidSubmission,
    JobAlreadyFinished,
    UnknownJob,
)
from agent_eval.execution.triggers import TriggerDef, load_triggers

__all__ = [
    "TERMINAL_STATUSES",
    "AgentAuth",
    "AgentConnectionProfile",
    "AgentConnectionView",
    "AgentHealthReport",
    "CronSpec",
    "DuplicateSubmission",
    "EvalPreset",
    "EvalRunJob",
    "EvalRunRequest",
    "EvalRunService",
    "FailureKind",
    "InvalidSubmission",
    "JobAlreadyFinished",
    "JobProgress",
    "JobRepository",
    "JobStatus",
    "LocalJobExecutor",
    "PresetRequest",
    "ScheduleDef",
    "SchedulerService",
    "TriggerDef",
    "UnknownJob",
    "check_agent_health",
    "load_agent_connections",
    "load_presets",
    "load_schedules",
    "load_triggers",
]
