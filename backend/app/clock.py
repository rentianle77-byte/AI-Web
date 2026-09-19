"""演示时钟:真实时间 + 可调偏移。

主动提醒/跟进依赖“第 3 天”“第 5 天”这类时间条件,比赛演示不可能真等三天,
所以所有业务时间都从这里取,演示时可以一键“快进 1 天”。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

_offset_seconds: int = 0


def get_offset() -> int:
    return _offset_seconds


def set_offset(seconds: int) -> None:
    global _offset_seconds
    _offset_seconds = int(seconds)


def advance(hours: float) -> int:
    set_offset(_offset_seconds + int(hours * 3600))
    return _offset_seconds


def now() -> datetime:
    """业务“当前时间”,naive UTC(数据库里统一存这个)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=_offset_seconds)


def now_local() -> datetime:
    return now().replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def fmt_local(dt: datetime | None, with_time: bool = True) -> str:
    local = to_local(dt)
    if local is None:
        return ""
    return local.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def iso(dt: datetime | None) -> str | None:
    local = to_local(dt)
    return local.isoformat() if local else None
