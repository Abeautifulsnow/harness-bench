"""Production Online Eval 自动化（P1-1：sampling / filter / auto / backfill / trend / alert）。

对齐语义（docs/web-evaluation-control-plane-design.md §61 的后续片）：
- **Definition Plane**：``evals/production-monitors/*.yaml``（过滤、采样率、policy 引用、
  告警与通知配置，随 Git 管理）；**Runtime State**：
  ``<data_root>/state/production-monitors/<id>.json``（cursor / 统计 / 告警台账，可重建）。
- **cursor 语义**：trace_id 形如 ``prod_<hex 毫秒><随机>``，字典序即时间序——cursor 之后
  （更新）的 trace 才进入本轮考虑；被过滤或采样淘汰的 trace 同样推进 cursor（决策是
  确定性的，重看只会得到同一个决定）。单次 tick 最多处理 ``backfill_batch`` 条，
  积压分多轮消化，不与摄取争 IO。
- **采样是幂等的**：按 trace_id 的 SHA-256 稳定哈希判定（内建 hash() 有进程级随机种子，
  会让同一 trace 在重启前后落进采样集的两个面），重启 / 回填不漂移。
- **告警分母**：fail 率只计 pass+fail（error 是基础设施事实、skipped 是观测不足，
  都不是质量结论——把 error 计入分母会让 judge 故障伪装成质量恶化）。
- **judge 调用复用 §61 同一条路径**（run_online_evaluation + 注入 adapter），
  评测结果带 ``triggered_by: monitor:<id>`` 落账，与手动评测同一事实源。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from yaml import SafeLoader
from yaml import load as _yaml_load

from agent_eval.errors import InvalidCallError
from agent_eval.evaluators.online_eval import (
    EvalPair,
    extract_pair,
    load_eval_policies,
    run_online_evaluation,
)
from agent_eval.execution.notifier import auth_headers, deliver
from agent_eval.execution.triggers import NotifyDef
from agent_eval.review.production_queue import queue_production_failures
from agent_eval.review.store import ReviewStore
from agent_eval.storage.production_store import ProductionStore

logger = logging.getLogger("agent_eval.production_monitors")

_MONITOR_ID_RE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


class MonitorFilter(BaseModel):
    """trace 级过滤：全部声明过的条件 AND；缺省字段 = 不过滤。"""

    source: str | None = None
    model: str | None = None
    # case_id 前缀：trace 内**全部** case 都须命中（trace 是评测原子，不拆半）
    case_id_prefix: str | None = None


class AlertDef(BaseModel):
    """单次自动评测的质量告警：metric 的 fail 率超阈值即投递 notify。"""

    metric: str
    max_fail_rate_percent: float = Field(gt=0, le=100)


class ProductionMonitorDef(BaseModel):
    id: str
    display_name: str
    enabled: bool = True
    description: str = ""
    filter: MonitorFilter = Field(default_factory=MonitorFilter)
    # 百分比 0~100；按 trace_id 稳定哈希，同一 trace 的去留与进程无关
    sampling_rate: float = Field(default=100.0, ge=0, le=100)
    policy: str
    # 单次 tick 最多评测的 trace 数（积压消化节奏，防止 judge 配额被打穿）
    backfill_batch: int = Field(default=20, ge=1)
    alert: AlertDef | None = None
    notify: NotifyDef | None = None
    # P1-2: auto-enqueue failing cases into the Review Queue (pending)
    enqueue_failures: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        # id 进 state 文件名与 evaluation 的 triggered_by，收紧字符集
        if not value or not set(value) <= _MONITOR_ID_RE_CHARS:
            raise ValueError(f"monitor id 只允许字母数字-下划线，got {value!r}")
        return value

    def matches(self, meta: dict) -> bool:
        f = self.filter
        if f.source is not None and meta.get("source") != f.source:
            return False
        if f.model is not None and meta.get("model") != f.model:
            return False
        if f.case_id_prefix is not None:
            case_ids = (meta.get("cases") or {}).keys()
            if not case_ids or not all(str(cid).startswith(f.case_id_prefix) for cid in case_ids):
                return False
        return True

    def sampled_in(self, trace_id: str) -> bool:
        if self.sampling_rate >= 100:
            return True
        if self.sampling_rate <= 0:
            return False
        digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % 10_000 < self.sampling_rate * 100


def load_production_monitors(evals_root: Path) -> list[ProductionMonitorDef]:
    directory = evals_root / "production-monitors"
    if not directory.is_dir():
        return []
    monitors: list[ProductionMonitorDef] = []
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            raw = _yaml_load(fh, SafeLoader)
        try:
            monitors.append(ProductionMonitorDef.model_validate(raw))
        except Exception as exc:
            raise InvalidCallError(f"invalid production monitor '{path.name}': {exc}") from exc
    return monitors


class MonitorStateStore:
    """``<data_root>/state/production-monitors/<id>.json``（原子写，scheduler 同款）。"""

    def __init__(self, data_root: Path) -> None:
        self.root = data_root / "state" / "production-monitors"
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, monitor_id: str) -> dict:
        path = self.root / f"{monitor_id}.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def save(self, monitor_id: str, state: dict) -> None:
        path = self.root / f"{monitor_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


class ProductionMonitorView(BaseModel):
    """API 出参：定义 + 运行台账合并视图。"""

    id: str
    display_name: str
    enabled: bool
    description: str = ""
    policy: str
    sampling_rate: float
    filter: MonitorFilter
    alert_metric: str | None = None
    enqueue_failures: bool = False
    cursor: str | None = None
    evaluated_total: int = 0
    queued_total: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None


class ProductionMonitorService:
    """tick 驱动（可注入时钟测试），可选守护线程周期 tick——SchedulerService 同款。"""

    def __init__(
        self,
        evals_root: Path,
        data_root: Path,
        evaluator: object,  # §61 注入契约：async evaluate(...) / version()
        *,
        review_store: ReviewStore | None = None,
        tick_interval_seconds: float = 60.0,
        now_fn: type[datetime] = datetime,
    ) -> None:
        self.evals_root = evals_root
        self.evaluator = evaluator
        self.review_store = review_store
        self.store = ProductionStore(data_root)
        self.states = MonitorStateStore(data_root)
        # tick 线程与 backfill 端点是 state 的两个并发写者：串行化 _process +
        # 台账落盘（原子写只防损坏，不防丢更新/cursor 回退）
        self._state_lock = threading.Lock()
        # breach 台账与 webhook 投递台账分开：alerts.jsonl 只有质量告警事实，
        # 投递尝试（成功/失败/重试）与 job 通知共用 notifications.jsonl 一个台账
        self.alert_log_path = data_root / "state" / "alerts.jsonl"
        self.delivery_log_path = data_root / "state" / "notifications.jsonl"
        self.tick_interval_seconds = tick_interval_seconds
        self.now_fn = now_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ queries

    def monitor_views(self) -> list[ProductionMonitorView]:
        views: list[ProductionMonitorView] = []
        for mdef in load_production_monitors(self.evals_root):
            state = self.states.get(mdef.id)
            views.append(
                ProductionMonitorView(
                    id=mdef.id,
                    display_name=mdef.display_name,
                    enabled=mdef.enabled,
                    description=mdef.description,
                    policy=mdef.policy,
                    sampling_rate=mdef.sampling_rate,
                    filter=mdef.filter,
                    alert_metric=mdef.alert.metric if mdef.alert else None,
                    enqueue_failures=mdef.enqueue_failures,
                    cursor=state.get("cursor"),
                    evaluated_total=int(state.get("evaluated_total") or 0),
                    queued_total=int(state.get("queued_total") or 0),
                    last_run_at=_as_dt(state.get("last_run_at")),
                    last_error=state.get("error"),
                )
            )
        return views

    # ------------------------------------------------------------------ tick

    def tick(self, now: datetime | None = None) -> list[str]:
        """考虑 cursor 之后的 trace；返回本轮产生了新评测的 monitor id。"""

        moment = now or self.now_fn.now().astimezone()
        fired: list[str] = []
        for mdef in load_production_monitors(self.evals_root):
            if not mdef.enabled:
                continue
            state = self.states.get(mdef.id)
            try:
                with self._state_lock:
                    evaluated = self._process(mdef, state, moment, batch=mdef.backfill_batch)
                    self.states.save(mdef.id, state)
            except Exception as exc:  # noqa: BLE001 - tick 崩了线程不能死
                state["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("production monitor %s failed: %s", mdef.id, exc)
                with self._state_lock:
                    self.states.save(mdef.id, state)
                continue
            if evaluated:
                fired.append(mdef.id)
        return fired

    def backfill(self, monitor_id: str, *, limit: int | None = None) -> dict:
        """显式回填：cursor 归零重扫，同步处理至多 limit 条（默认 5×batch）。"""

        monitors = {m.id: m for m in load_production_monitors(self.evals_root)}
        mdef = monitors.get(monitor_id)
        if mdef is None:
            raise InvalidCallError(f"unknown production monitor: {monitor_id}")
        if limit is not None and limit < 1:
            raise InvalidCallError("backfill limit must be >= 1")
        state = self.states.get(monitor_id)
        batch = limit if limit is not None else mdef.backfill_batch * 5
        with self._state_lock:
            state["cursor"] = None  # 回填重扫全部历史
            evaluated = self._process(mdef, state, self.now_fn.now().astimezone(), batch=batch)
            state["error"] = None
            self.states.save(monitor_id, state)
        return {"monitor": monitor_id, "evaluated": evaluated, "considered_cap": batch}

    # ------------------------------------------------------------------ core

    def _process(
        self, mdef: ProductionMonitorDef, state: dict, moment: datetime, *, batch: int
    ) -> int:
        """按时间序处理 cursor 之后的 trace（每条都推进 cursor）；返回评测条数。"""

        cursor = state.get("cursor")
        pending = [
            meta for meta in self.store.list() if cursor is None or meta["trace_id"] > cursor
        ]
        pending.reverse()  # 旧 → 新，回填语义与摄取顺序一致
        evaluated = 0
        for meta in pending[:batch]:
            trace_id = meta["trace_id"]
            state["cursor"] = trace_id
            if not mdef.matches(meta) or not mdef.sampled_in(trace_id):
                continue
            result = self._evaluate(mdef, meta, moment)
            if result is not None:
                evaluated += 1
                state["evaluated_total"] = int(state.get("evaluated_total") or 0) + 1
                self._check_alert(mdef, result, meta["trace_id"], moment)
                if mdef.enqueue_failures and self.review_store is not None:
                    queued = queue_production_failures(
                        self.review_store, trace_id=meta["trace_id"], evaluation=result
                    )
                    if queued:
                        state["queued_total"] = int(state.get("queued_total") or 0) + len(queued)
            state["last_run_at"] = moment.isoformat()
            state["error"] = None
        return evaluated

    def _evaluate(self, mdef: ProductionMonitorDef, meta: dict, moment: datetime) -> dict | None:
        """对单条 trace 跑 policy；无可评测 pair 返回 None（trace 计入但不产评测）。"""

        policies = {p.id: p for p in load_eval_policies(self.evals_root)}
        policy = policies.get(mdef.policy)
        if policy is None:
            raise InvalidCallError(f"monitor '{mdef.id}': unknown eval policy '{mdef.policy}'")
        trace_id = meta["trace_id"]
        pairs: list[EvalPair] = []
        unextractable: list[str] = []
        for case_id, entry in (meta.get("cases") or {}).items():
            pair = extract_pair(case_id, entry, self.store.load_events(trace_id, case_id))
            if pair is None:
                unextractable.append(case_id)
            else:
                pairs.append(pair)
        if not pairs:
            return None
        result = asyncio.run(
            run_online_evaluation(
                pairs=pairs,
                policy=policy,
                adapter=self.evaluator,
                unextractable_cases=unextractable,
            )
        )
        result["triggered_by"] = f"monitor:{mdef.id}"
        self.store.save_evaluation(trace_id, result)
        return result

    def _check_alert(
        self, mdef: ProductionMonitorDef, result: dict, trace_id: str, moment: datetime
    ) -> None:
        if mdef.alert is None:
            return
        bucket = (result["summary"].get("metrics") or {}).get(mdef.alert.metric)
        if bucket is None:
            return
        base = bucket.get("pass", 0) + bucket.get("fail", 0)
        if base == 0:
            return  # error/skipped 不进分母：judge 故障不得伪装成质量恶化
        fail_rate_percent = bucket.get("fail", 0) / base * 100
        if fail_rate_percent <= mdef.alert.max_fail_rate_percent:
            return
        record = {
            "monitor": mdef.id,
            "metric": mdef.alert.metric,
            "fail_rate_percent": round(fail_rate_percent, 2),
            "threshold_percent": mdef.alert.max_fail_rate_percent,
            "trace_id": trace_id,
            "evaluation_id": result.get("evaluation_id"),
            "fired_at": moment.isoformat(),
        }
        _append_jsonl(self.alert_log_path, record)
        if mdef.notify is not None and mdef.notify.webhook:
            payload = {
                "kind": "production_monitor_alert",
                **record,
                "summary": result.get("summary"),
            }
            # 尽力而为：告警投递失败留痕（notifier 重试语义），不阻塞 tick 线程
            threading.Thread(
                target=deliver,
                args=(
                    mdef.notify.webhook,
                    payload,
                    auth_headers(mdef.notify.token_secret_ref),
                    self.delivery_log_path,
                ),
                name=f"monitor-alert-{mdef.id}",
                daemon=True,
            ).start()

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="production-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_interval_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - tick 崩了线程不能死
                logger.warning("production monitor tick failed: %s", exc)


# ------------------------------------------------------------------ trends


def online_eval_trend(store: ProductionStore, *, days: int = 30) -> list[dict]:
    """跨 trace 聚合 Online Eval 结论：按 (日期, metric) 给出 pass/fail/error/skipped。"""

    since = (datetime.now().astimezone() - timedelta(days=days)).date()
    buckets: dict[tuple[str, str], dict] = {}
    for meta in store.list():
        trace_id = meta["trace_id"]
        for evaluation in store.list_evaluations(trace_id):
            created = str(evaluation.get("created_at") or "")
            day = created[:10]
            if not day or day < since.isoformat():
                continue
            for row in evaluation.get("rows") or []:
                key = (day, str(row.get("metric")))
                bucket = buckets.setdefault(
                    key,
                    {
                        "date": day,
                        "metric": key[1],
                        "pass": 0,
                        "fail": 0,
                        "error": 0,
                        "skipped": 0,
                        "scores": [],
                    },
                )
                verdict = str(row.get("verdict"))
                if verdict in bucket:
                    bucket[verdict] += 1
                if row.get("score") is not None:
                    bucket["scores"].append(row["score"])
    out = []
    for key in sorted(buckets, reverse=True):
        bucket = buckets[key]
        scores = bucket.pop("scores")
        bucket["mean_score"] = round(sum(scores) / len(scores), 4) if scores else None
        out.append(bucket)
    return out


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _as_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
