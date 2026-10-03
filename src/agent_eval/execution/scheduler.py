"""Scheduled Evaluation（V2，docs §52 的旗舰场景：Nightly）。

Definition Plane：``evals/schedules/*.yaml``（cron、preset/request 引用、通知配置，
随 Git 管理）。Runtime State：``<data_root>/state/schedules/<id>.json``
（last_run_at / last_job_id / next_run_at / error——可重建的调度台账）。

语义决策（§58 有记录）：
- 触发即提交：scheduler 只负责"何时调 ``EvalRunService.submit``"，评测语义全部
  复用执行面（校验、并发、Runner）——schedule 命中但定义非法时，错误记进
  调度台账而不是炸掉线程；
- 错过的调度（进程停机跨过触发点）**合并为一次**立即补跑（coalesce），
  不回填历史；
- 本地时间 cron；DST 折返日的"少跑/多跑一次"是已知边界。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, field_validator
from yaml import SafeLoader
from yaml import load as _yaml_load

from agent_eval.errors import InvalidCallError
from agent_eval.execution.cron import CronSpec
from agent_eval.execution.models import EvalRunRequest
from agent_eval.execution.presets import (
    PresetRequest,
    load_presets,
    resolve_request,
)
from agent_eval.execution.triggers import NotifyDef

logger = logging.getLogger("agent_eval.scheduler")


class ScheduleDef(BaseModel):
    id: str
    display_name: str
    enabled: bool = True
    description: str = ""
    cron: str
    preset: str | None = None
    request: PresetRequest | None = None
    notify: NotifyDef | None = None

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        CronSpec.parse(value)  # 坏表达式在装载期就炸，而不是第一次 tick
        return value


class ScheduleView(BaseModel):
    """API 出参：定义 + 调度台账的合并视图。"""

    id: str
    display_name: str
    enabled: bool
    description: str = ""
    cron: str
    preset: str | None = None
    last_run_at: datetime | None = None
    last_job_id: str | None = None
    next_run_at: datetime | None = None
    error: str | None = None


def load_schedules(evals_root: Path) -> list[ScheduleDef]:
    directory = evals_root / "schedules"
    if not directory.is_dir():
        return []
    schedules: list[ScheduleDef] = []
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            raw = _yaml_load(fh, SafeLoader)
        try:
            schedules.append(ScheduleDef.model_validate(raw))
        except Exception as exc:
            raise InvalidCallError(f"invalid schedule '{path.name}': {exc}") from exc
    return schedules


class ScheduleStateStore:
    """调度台账：``<data_root>/state/schedules/<id>.json``（原子写）。"""

    def __init__(self, data_root: Path) -> None:
        self.root = data_root / "state" / "schedules"
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, schedule_id: str) -> dict:
        path = self.root / f"{schedule_id}.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def save(self, schedule_id: str, state: dict) -> None:
        path = self.root / f"{schedule_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


class SchedulerService:
    """定时器：tick 驱动（可注入时钟测试），可选守护线程周期 tick。"""

    def __init__(
        self,
        evals_root: Path,
        data_root: Path,
        service,  # EvalRunService；类型注解留字符串避免环 import
        *,
        tick_interval_seconds: float = 30.0,
        now_fn: type[datetime] = datetime,
    ) -> None:
        self.evals_root = evals_root
        self.service = service
        self.states = ScheduleStateStore(data_root)
        self.tick_interval_seconds = tick_interval_seconds
        self.now_fn = now_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ queries

    def schedule_views(self) -> list[ScheduleView]:
        views: list[ScheduleView] = []
        for sdef in load_schedules(self.evals_root):
            state = self.states.get(sdef.id)
            views.append(
                ScheduleView(
                    id=sdef.id,
                    display_name=sdef.display_name,
                    enabled=sdef.enabled,
                    description=sdef.description,
                    cron=sdef.cron,
                    preset=sdef.preset,
                    last_run_at=_as_dt(state.get("last_run_at")),
                    last_job_id=state.get("last_job_id"),
                    next_run_at=_as_dt(state.get("next_run_at")),
                    error=state.get("error"),
                )
            )
        return views

    # ------------------------------------------------------------------ trigger

    def tick(self, now: datetime | None = None) -> list[str]:
        """检查全部 enabled 调度，提交到期的；返回本次触发的 schedule id。"""

        moment = now or self.now_fn.now().astimezone()
        presets = {p.id: p for p in load_presets(self.evals_root)}
        fired: list[str] = []
        for sdef in load_schedules(self.evals_root):
            if not sdef.enabled:
                continue
            state = self.states.get(sdef.id)
            next_at = _as_dt(state.get("next_run_at"))
            if next_at is None:
                # 首次见到该调度：从现在起算下一次，绝不为了"补上周"连发历史
                next_at = CronSpec.parse(sdef.cron).next_after(moment)
                self._save(sdef, state, next_run_at=next_at)
                continue
            if moment < next_at:
                continue
            cron = CronSpec.parse(sdef.cron)
            self._submit(sdef, presets, state, moment)
            self._save(sdef, state, next_run_at=cron.next_after(moment))
            fired.append(sdef.id)
        return fired

    def _submit(self, sdef: ScheduleDef, presets: dict, state: dict, moment: datetime) -> None:
        try:
            preset_obj = presets.get(sdef.preset) if sdef.preset else None
            if sdef.preset and preset_obj is None:
                raise InvalidCallError(f"schedule '{sdef.id}': unknown preset '{sdef.preset}'")
            request = EvalRunRequest.model_validate(
                resolve_request(preset_obj.request if preset_obj else PresetRequest(), sdef.request)
            )
            notify = sdef.notify
            job = self.service.submit(
                request,
                requested_by=f"schedule:{sdef.id}",
                notify_webhook=notify.webhook if notify else None,
                notify_token_ref=notify.token_secret_ref if notify else None,
            )
            state["last_run_at"] = moment.isoformat()
            state["last_job_id"] = job.job_id
            state["error"] = None
        except Exception as exc:  # noqa: BLE001 - 调度线程必须活着，错误记台账
            state["last_run_at"] = moment.isoformat()
            state["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("schedule %s failed to submit: %s", sdef.id, exc)

    def _save(self, sdef: ScheduleDef, state: dict, **updates) -> None:
        serialized = {
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in updates.items()
        }
        state.update(serialized)
        self.states.save(sdef.id, state)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="eval-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_interval_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - tick 崩了线程不能死
                logger.warning("scheduler tick failed: %s", exc)


def _as_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
