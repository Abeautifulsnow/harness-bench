"""Job 实时事件 Hub（§18 细粒度事件：case/turn/tool 级）。

拓扑：Runner 跑在**执行线程自己的 loop** 上（LocalJobExecutor），SSE 订阅者
挂在 **uvicorn 请求 loop** 上——publish 必须跨线程。实现：每个订阅记录
(订阅者 loop, 队列)，publish 经 ``loop.call_soon_threadsafe`` 投递。

背压策略：**有界队列 + 丢弃**（不阻塞执行线程、不无界占内存）。事件是
best-effort 观测面；评测事实与进度以 Job 快照为准（§35 的分工不变）。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

_QUEUE_SIZE = 500


class JobEventHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, job_id: str) -> asyncio.Queue:
        """订阅一个 Job 的事件流。返回的队列属于**调用方所在的 loop**。"""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        with self._lock:
            self._subs.setdefault(job_id, set()).add((loop, queue))
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        # 按 queue 身份查找（review #S01）：退订可能发生在与订阅不同的
        # loop 上下文（生成器被 GC、测试收尾），重取 running loop 会拼错键。
        with self._lock:
            subscribers = self._subs.get(job_id)
            if not subscribers:
                return
            self._subs[job_id] = {record for record in subscribers if record[1] is not queue}
            if not self._subs[job_id]:
                del self._subs[job_id]

    def publish(self, job_id: str, event: dict) -> None:
        """向 Job 的全部订阅者投递事件。可在任意线程调用；慢订阅者丢帧。"""

        with self._lock:
            subscribers = list(self._subs.get(job_id, ()))
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                # 订阅者的 loop 已关闭（页面断开、测试收尾）：清理死订阅。
                with self._lock:
                    subscribers = self._subs.get(job_id)
                    if subscribers:
                        self._subs[job_id] = {
                            record for record in subscribers if record[1] is not queue
                        }

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict) -> None:
        # 慢订阅者：丢帧保执行线程（事实在 Job 快照里，不在这条帧上）。
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)
