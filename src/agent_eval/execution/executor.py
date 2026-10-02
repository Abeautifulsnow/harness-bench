"""LocalJobExecutor（设计文档 §23/§24/§25）：本地 Job 执行机制。

为什么是独立线程 + 独立 asyncio loop，而不是在 FastAPI 请求的 loop 上 spawn task：
任务生命周期必须独立于接收请求的 loop（§35——评测不依赖浏览器、也不依赖某一次
HTTP 请求的存续；TestClient 与多 worker 部署下"请求 loop"甚至不止一个）。

机制：
- 每个 Job 一个 task，经 ``run_coroutine_threadsafe`` 提交到执行 loop；
- 全局并发由该 loop 上的 ``Semaphore(max_running_jobs)`` 控制（§24 的第一层；
  Runner 内部的 agent/judge 并发是第二层，两层缺一不可）；
- 超过并发数的 Job 停在信号量上（QUEUED，§25）；
- 取消：concurrent Future.cancel() 会传播进底层 task —— 无论在排队还是执行中，
  CancelledError 都会被触发，Runner 收到真实 cancellation（§17）。

服务层状态变更（QUEUED→RUNNING→…）发生在 body 内部；本类只负责机制，不碰语义。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any


class LocalJobExecutor:
    def __init__(
        self,
        max_running_jobs: int = 2,
        on_cancelled: Callable[[str], None] | None = None,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self._max = max(1, max_running_jobs)
        self._on_cancelled = on_cancelled
        self._on_error = on_error

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._sem: asyncio.Semaphore | None = None
        self._futures: dict[str, Future[None]] = {}

    # ------------------------------------------------------------------ API

    def submit(self, job_id: str, body: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """提交一个 Job body（协程工厂，在执行 loop 上才真正实例化）。"""

        self._ensure_thread()
        assert self._loop is not None and self._sem is not None

        async def guarded() -> None:
            try:
                # 信号量获取也在 try 里：排队中（等槽位）被取消时，
                # CancelledError 同样要收口成 CANCELLED，而不是裸抛。
                async with self._sem:
                    await body()
            except asyncio.CancelledError:
                # 排队中被取消与执行中被取消都走这里：cancellation 是正常
                # 结局而不是事故，交给服务层落成 CANCELLED。
                if self._on_cancelled is not None:
                    self._on_cancelled(job_id)
            except Exception as exc:  # noqa: BLE001 - body 自身已兜底的兜底
                if self._on_error is not None:
                    self._on_error(job_id, exc)

        future = asyncio.run_coroutine_threadsafe(guarded(), self._loop)
        self._futures[job_id] = future
        future.add_done_callback(lambda _completed, jid=job_id: self._futures.pop(jid, None))

    def cancel(self, job_id: str) -> bool:
        """请求取消。返回 False = 没有可取消的在途任务（已完成/已清理）。"""

        future = self._futures.get(job_id)
        if future is None or future.done():
            return False
        return future.cancel()

    def is_in_flight(self, job_id: str) -> bool:
        future = self._futures.get(job_id)
        return future is not None and not future.done()

    def in_flight_ids(self) -> set[str]:
        return {job_id for job_id, pending in self._futures.items() if not pending.done()}

    def shutdown(self) -> None:
        for jid in list(self._futures):
            self.cancel(jid)
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)

    # -------------------------------------------------------------- internals

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="eval-job-executor", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("eval job executor thread failed to start")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._sem = asyncio.Semaphore(self._max)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()
