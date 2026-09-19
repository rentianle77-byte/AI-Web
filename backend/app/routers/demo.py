"""演示控制:快进时钟、一键场景、系统自检。

比赛现场没法真等三天,靠快进时钟把「第 3 天主动提醒」在 10 秒内演出来。
"""
from __future__ import annotations

from fastapi import APIRouter

from .. import clock
from ..config import settings
from ..db import SessionLocal, db_kind
from ..events import bus
from ..followup import scheduler
from ..llm.factory import get_client
from ..rag.index import index
from ..tasks import service
from .schemas import AdvanceClockIn

router = APIRouter(prefix="/api/demo", tags=["demo"])


@router.get("/status")
def status():
    client = get_client()
    session = SessionLocal()
    try:
        pending = len(service.list_followups(session, status="scheduled"))
        active = len([t for t in service.list_tasks(session) if t.status in ("active", "waiting", "attention")])
    finally:
        session.close()
    return {
        "llm": {"provider": client.provider, "model": client.model, "is_mock": client.provider == "mock"},
        "database": {"kind": db_kind(), "url": settings.database_url.split("@")[-1]},
        "knowledge": {"docs": len(index.docs), "chunks": len(index.chunks), "embedding": index.embedding_enabled},
        "clock": {"now": clock.fmt_local(clock.now()), "offset_hours": round(clock.get_offset() / 3600, 2)},
        "scheduler": {"running": scheduler.is_running(), "interval_seconds": settings.followup_interval, "pending_followups": pending},
        "tasks": {"active": active},
    }


@router.get("/clock")
def get_clock():
    return {"now": clock.fmt_local(clock.now()), "date": clock.now_local().strftime("%Y-%m-%d"), "offset_hours": round(clock.get_offset() / 3600, 2)}


@router.post("/clock/advance")
async def advance_clock(body: AdvanceClockIn):
    """快进时间,并立刻扫描一次到期跟进 —— 演示「第 3 天主动提醒」用。"""
    clock.advance(body.hours)
    fired = await scheduler.run_once()
    payload = {"now": clock.fmt_local(clock.now()), "offset_hours": round(clock.get_offset() / 3600, 2), "followups_fired": fired}
    bus.publish({"type": "clock_advanced", **payload})
    return payload


@router.post("/clock/reset")
def reset_clock():
    clock.set_offset(0)
    bus.publish({"type": "clock_advanced", "now": clock.fmt_local(clock.now()), "offset_hours": 0, "followups_fired": 0})
    return {"now": clock.fmt_local(clock.now()), "offset_hours": 0}


@router.get("/scenarios")
def scenarios():
    """首页的一键演示场景。"""
    return [
        {
            "id": "claim_damaged",
            "agent": "claim",
            "icon": "📦",
            "title": "网购杯子寄到就碎了",
            "desc": "索赔 Agent:查轨迹 → 定责任方 → 算赔偿 → 出话术 → 排跟进",
            "message": "我在淘宝买了一套玻璃杯,780 块,中通寄过来的,单号 73121234567,昨天签收拆开发现碎了两个。当时没保价。帮我索赔。",
        },
        {
            "id": "claim_lost",
            "agent": "claim",
            "icon": "🔍",
            "title": "快递十几天没动静",
            "desc": "按「彻底延误」时限主张按丢失赔付,并升级 12305 申诉",
            "message": "我自己寄的快递,单号 SF1234567890,从广州寄到西安,已经 12 天了物流一直停在广州转运中心,里面是台二手相机,值 3000 块,保价了 3000。怎么办?",
        },
        {
            "id": "return_shoes",
            "agent": "return",
            "icon": "👟",
            "title": "鞋子不合脚要退货",
            "desc": "退货 Agent:判资格 → 算运费 → 出话术 → 第 3 天问签收、第 5 天问退款",
            "message": "三天前在天猫买的运动鞋,349 块,昨天收到试了下不合脚,吊牌都还在,有运费险。我在杭州,商家在泉州。帮我退掉。",
        },
        {
            "id": "return_quality",
            "agent": "return",
            "icon": "⚠️",
            "title": "收到的东西有质量问题",
            "desc": "质量问题不受七天限制,运费归商家,Agent 会替你把这点讲明白",
            "message": "拼多多买的加湿器,159 块,用了两天就不出雾了,商家让我自己出运费寄回去检测。我不想出这个钱,有道理吗?",
        },
        {
            "id": "ship_fragile",
            "agent": "ship",
            "icon": "🚚",
            "title": "寄易碎品选哪家",
            "desc": "寄件 Agent:算体积重 → 三方案对比 → 下单指引 → 打包避坑",
            "message": "我要从杭州寄一个陶瓷花瓶到成都,大概 2 公斤,箱子 40×30×30 厘米,值 600 块,不急。哪家划算又稳妥?",
        },
        {
            "id": "ship_remote",
            "agent": "ship",
            "icon": "🏔️",
            "title": "往新疆寄大件被子",
            "desc": "偏远地区加价与体积重的双重坑,Agent 会算给你看",
            "message": "从上海寄一床冬被到新疆喀什,实重 4 公斤,压缩袋装了还有 60×50×40 厘米。要多少钱?",
        },
    ]


@router.post("/reset")
def reset_all(keep_knowledge: bool = True):
    """清空演示数据(对话/任务/跟进/材料),知识库不动。"""
    from ..models import Conversation, Followup, Material, Message, Task

    session = SessionLocal()
    try:
        counts = {}
        for model in (Message, Followup, Material, Task, Conversation):
            counts[model.__tablename__] = session.query(model).delete()
        session.commit()
        clock.set_offset(0)
        bus.publish({"type": "reset"})
        return {"deleted": counts, "clock_reset": True}
    finally:
        session.close()
