"""进程内事件总线:后台跟进 / 任务变更 → 推给前端(SSE)。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# 每个订阅者的缓冲深度。前端正常消费的话几乎用不到,
# 主要挡的是"标签页被切到后台、浏览器降频"这类临时卡顿。
QUEUE_SIZE = 500


class EventBus:
    """一对多事件广播。

    关于满队列的处理(测试用例 GT-08):
    早先的实现是 put_nowait 失败就静默丢弃**最新**事件,有两个问题 ——
    丢掉的恰恰是最新状态,前端会停留在过期画面;而且丢了没人知道。

    现在改成丢**最旧**的:新事件一定能进去,前端拿到的永远是最新状态;
    同时累计丢弃数,健康检查里能看到。
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._dropped = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 腾出最旧的那条再放新的。宁可让订阅者漏掉一条历史事件,
                # 也不能让它停在过期状态上。
                try:
                    q.get_nowait()
                    q.task_done()
                except (asyncio.QueueEmpty, ValueError):
                    pass
                self._dropped += 1
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning("事件队列腾位后仍然放不进去,丢弃:%s", event.get("type"))

    @property
    def dropped(self) -> int:
        """累计丢弃的事件数。非 0 说明有订阅者消费不过来。"""
        return self._dropped

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def stats(self) -> dict[str, int]:
        return {
            "subscribers": len(self._subscribers),
            "dropped": self._dropped,
            "queue_size": QUEUE_SIZE,
        }


bus = EventBus()
