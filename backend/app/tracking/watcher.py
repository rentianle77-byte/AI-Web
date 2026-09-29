"""包裹监控:系统自己盯着物流,状态一变就主动告诉用户。

和「用户问了才查」的区别很大:
  查询  —— 用户得记得回来问
  监控  —— 包裹卡住三天、派送异常、签收了,系统自己发现并通知

实现上用轮询而不是 webhook:
  · 快递100 的订阅推送要求服务器有公网回调地址且已备案,门槛高
  · 轮询对演示数据同样有效,不依赖任何外部配置
  · 这个场景对实时性要求很低,几分钟一次完全够
配了快递100 密钥时,轮询拿到的就是真实轨迹;没配就是演示数据。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..events import bus
from ..models import TrackingWatch
from .provider import query_tracking

log = logging.getLogger(__name__)

# 多久没更新算「卡住了」
STALE_DAYS = 3

# 值得主动打扰用户的状态变化
NOTABLE = {
    "delivered": "已签收",
    "exception": "派送异常",
    "stalled": "物流停滞",
    "damaged": "轨迹出现破损记录",
}


def watch(session: Session, tracking_no: str, company: str | None, task_id: str | None, conversation_id: str | None) -> TrackingWatch:
    """开始盯一个包裹。同一个单号只盯一次。"""
    existing = session.scalar(select(TrackingWatch).where(TrackingWatch.tracking_no == tracking_no, TrackingWatch.status != "stopped"))
    if existing:
        existing.task_id = task_id or existing.task_id
        existing.conversation_id = conversation_id or existing.conversation_id
        session.flush()
        return existing
    w = TrackingWatch(
        tracking_no=tracking_no,
        company=company,
        task_id=task_id,
        conversation_id=conversation_id,
        status="watching",
        last_state="",
        last_event_time="",
    )
    session.add(w)
    session.flush()
    return w


def unwatch(session: Session, tracking_no: str) -> int:
    n = 0
    for w in session.scalars(select(TrackingWatch).where(TrackingWatch.tracking_no == tracking_no, TrackingWatch.status != "stopped")):
        w.status = "stopped"
        n += 1
    return n


def _days_since(text: str) -> float | None:
    """轨迹时间距今多少天。解析不了就返回 None,不要因此报错。"""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.strip()[: len(fmt) + 2], fmt)
            break
        except ValueError:
            continue
    else:
        return None
    local_now = clock.now_local().replace(tzinfo=None)
    return (local_now - dt).total_seconds() / 86400


def _classify(info: dict) -> tuple[str, str]:
    """把一次查询结果归成 (状态码, 给用户看的说法)。"""
    if info.get("is_delivered"):
        return "delivered", f"已签收({info.get('company','')} {info.get('tracking_no','')})"
    events = info.get("events") or []
    latest = events[0].get("desc", "") if events else ""
    if any(k in latest for k in ("异常", "退回", "无法联系", "拒收", "滞留")):
        return "exception", f"派送异常:{latest}"
    if any(k in latest for k in ("破损", "损坏")):
        return "damaged", f"轨迹出现破损记录:{latest}"
    stale = info.get("days_since_last_update")
    if stale is None and events:
        stale = _days_since(events[0].get("time", ""))
    if stale is not None and stale >= STALE_DAYS:
        return "stalled", f"物流已 {stale:.0f} 天没有更新,可能卡住了"
    return "in_transit", latest or "运输中"


def check_one(session: Session, w: TrackingWatch) -> dict | None:
    """查一次,有值得说的变化就返回变更详情,否则返回 None。"""
    try:
        info = query_tracking(w.tracking_no, w.company)
    except Exception:  # noqa: BLE001  —— 单个包裹查失败不能影响别的
        log.exception("查询 %s 失败", w.tracking_no)
        return None

    state, human = _classify(info)
    events = info.get("events") or []
    latest_time = events[0].get("time", "") if events else ""

    w.last_checked_at = clock.now()
    w.last_state = state
    # 轨迹没动就不算变化,避免同一个状态反复打扰
    unchanged = state == w.last_state and latest_time == w.last_event_time
    first_time_notable = state in NOTABLE and latest_time != w.last_event_time
    w.last_event_time = latest_time

    if state in ("delivered",):
        w.status = "stopped"  # 签收了就不用再盯
    session.flush()

    if not first_time_notable or unchanged:
        return None
    return {
        "tracking_no": w.tracking_no,
        "company": info.get("company"),
        "state": state,
        "label": NOTABLE.get(state, state),
        "detail": human,
        "task_id": w.task_id,
        "conversation_id": w.conversation_id,
        "source": info.get("source"),
    }


def check_all(session: Session) -> list[dict]:
    """扫一遍所有在盯的包裹,返回有变化的。"""
    changes = []
    for w in session.scalars(select(TrackingWatch).where(TrackingWatch.status == "watching")):
        change = check_one(session, w)
        if change:
            changes.append(change)
    return changes


def announce(change: dict) -> None:
    """把变化推给前端,并通知用户。"""
    from ..notify import send_notification

    bus.publish({"type": "tracking_change", **change})
    subject = f"【快递管家】{change['label']} · {change['tracking_no']}"
    body = f"{change['detail']}\n\n单号:{change['tracking_no']}({change.get('company') or ''})"
    if change.get("source") == "mock":
        body += "\n\n(演示环境数据)"
    try:
        send_notification(subject, body)
    except Exception:  # noqa: BLE001
        log.exception("包裹变化通知发送失败")
