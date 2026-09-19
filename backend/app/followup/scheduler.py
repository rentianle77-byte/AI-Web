"""主动跟进调度器:后台循环扫描到期的跟进,自动触发 Agent 汇报。

这是"主动提醒 / 跟进"这一格的实现 —— 用户不用回来问,到点我们主动开口。
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from .. import clock
from ..config import settings
from ..db import SessionLocal
from ..models import Followup

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_running = False


def due_followup_ids() -> list[int]:
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(Followup).where(Followup.status == "scheduled", Followup.due_at <= clock.now()).order_by(Followup.due_at.asc())
        )
        return [f.id for f in rows]
    finally:
        session.close()


async def _tick() -> int:
    from ..agent.runtime import run_followup

    fired = 0
    for fid in due_followup_ids():
        try:
            log.info("触发跟进 #%s", fid)
            await run_followup(fid)
            fired += 1
        except Exception:  # noqa: BLE001
            log.exception("跟进 #%s 执行失败", fid)
            session = SessionLocal()
            try:
                f = session.get(Followup, fid)
                if f and f.status == "fired":
                    f.status = "scheduled"  # 放回队列,下轮重试
                    session.commit()
            finally:
                session.close()
    return fired


async def _loop() -> None:
    global _running
    _running = True
    log.info("跟进调度器启动,每 %s 秒扫描一次", settings.followup_interval)
    try:
        while _running:
            try:
                await _tick()
            except Exception:  # noqa: BLE001
                log.exception("跟进扫描出错")
            await asyncio.sleep(settings.followup_interval)
    except asyncio.CancelledError:
        pass
    finally:
        _running = False
        log.info("跟进调度器停止")


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task, _running
    _running = False
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


async def run_once() -> int:
    """手动触发一次扫描(演示"快进时间"后立刻生效)。"""
    return await _tick()


def is_running() -> bool:
    return _running
