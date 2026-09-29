"""外部动作能力:通知触达、包裹监控、寄件下单。

这三样是把产品从「给建议」推向「真办事」的关键。
之前所有工具都只作用于自己的数据库,对外部世界零副作用 ——
那确实很接近套壳,只是组织得整齐。
"""
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
from app.db import Base
from app.models import Conversation, TrackingWatch


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    import app.notify.service as notify_svc

    monkeypatch.setattr(notify_svc, "SessionLocal", maker)
    s = maker()
    yield s
    s.close()


# ---------------- 通知触达 ----------------
def test_channels_report_what_is_missing():
    """没配置时要说清楚缺什么,不能只说「不可用」。"""
    from app.notify import available_channels

    chans = available_channels()
    assert {c["name"] for c in chans} == {"email", "wechat", "webhook"}
    for c in chans:
        if not c["available"]:
            assert c["reason"], f"{c['name']} 不可用却没说原因"


def test_send_without_config_is_explicit_not_silent():
    """没配通道时要明确报告,不能假装发成功。"""
    from app.notify import send_notification

    r = send_notification("主题", "正文")
    assert r.sent is False
    assert "没有配置任何通知通道" in r.summary()


def test_contact_persists(session):
    from app.notify.service import get_contact, set_contact

    set_contact("someone@example.com")
    assert get_contact() == "someone@example.com"


def test_notify_failure_never_breaks_followup(session, monkeypatch):
    """通知挂了绝不能影响跟进本身 —— 跟进是主流程,通知是附加。"""
    import app.agent.runtime as runtime

    def boom(*a, **kw):
        raise RuntimeError("SMTP 炸了")

    monkeypatch.setattr("app.notify.notify_followup", boom, raising=False)
    # 不抛异常就算过
    runtime._notify_followup_result("T-X", "c_1", "该跟进了", [])


# ---------------- 包裹监控 ----------------
def test_watch_is_idempotent(session):
    from app.tracking import watcher

    c = Conversation(title="t", agent_type="claim")
    session.add(c)
    session.commit()
    a = watcher.watch(session, "73121234567", "中通", None, c.id)
    b = watcher.watch(session, "73121234567", "中通", None, c.id)
    session.commit()
    assert a.id == b.id, "同一个单号不该重复建监控"
    assert session.scalar(select(TrackingWatch).where(TrackingWatch.tracking_no == "73121234567")) is not None


def test_unwatch_stops_it(session):
    from app.tracking import watcher

    watcher.watch(session, "999", "中通", None, None)
    session.commit()
    assert watcher.unwatch(session, "999") == 1
    session.commit()
    w = session.scalar(select(TrackingWatch).where(TrackingWatch.tracking_no == "999"))
    assert w.status == "stopped"


def test_check_all_detects_notable_changes(session):
    """巡检要能发现签收 / 异常 / 停滞,这是「替你盯着」的实质。"""
    from app.tracking import watcher

    # 演示数据按单号哈希分配场景,多铺几个必然覆盖到值得通知的
    for no in ("73100000001", "73100000002", "73100000003", "SF1234567890", "73199999999", "4312345678901"):
        watcher.watch(session, no, None, None, None)
    session.commit()
    changes = watcher.check_all(session)
    session.commit()
    assert changes, "六个不同场景的包裹里应该至少有一个值得通知"
    for ch in changes:
        assert ch["state"] in ("delivered", "exception", "stalled", "damaged")
        assert ch["detail"]


def test_delivered_package_stops_being_watched(session):
    """签收了就不用再盯,否则监控列表只增不减。"""
    from app.tracking import watcher

    w = watcher.watch(session, "73100000001", None, None, None)
    session.commit()
    for _ in range(3):
        watcher.check_one(session, w)
    session.commit()
    if w.last_state == "delivered":
        assert w.status == "stopped"


def test_same_state_not_reported_twice(session):
    """同一个状态不能反复打扰用户。"""
    from app.tracking import watcher

    w = watcher.watch(session, "73199999999", None, None, None)
    session.commit()
    first = watcher.check_one(session, w)
    session.commit()
    second = watcher.check_one(session, w)
    session.commit()
    if first:
        assert second is None, "同一个状态第二次不该再报"


# ---------------- 寄件下单 ----------------
def test_demo_order_is_clearly_marked():
    """演示下单必须显眼标注,绝不能让用户以为快递员真会上门。"""
    from app.shipping.order import place_order

    r = place_order(from_place="杭州", to_place="成都", weight_kg=2, item_type="花瓶", sender={}, receiver={})
    assert r.ok and r.is_demo
    assert r.tracking_no.startswith("DEMO")
    d = r.to_dict()
    assert "⚠ 演示" in d
    assert "没有真实发出" in d["⚠ 演示"]


def test_demo_order_reuses_the_quote_engine():
    """演示下单的价格要来自真实比价引擎,不能随便编一个数。"""
    from app.domain.shipping import estimate
    from app.shipping.order import place_order

    q = estimate("杭州", "成都", 2, item_type="陶瓷花瓶", declared_value=600)
    r = place_order(from_place="杭州", to_place="成都", weight_kg=2, item_type="陶瓷花瓶",
                    declared_value=600, sender={}, receiver={})
    assert r.estimated_fee == q["recommended"]["total"]


def test_real_provider_status_explains_requirement():
    from app.shipping.order import has_real_provider, provider_status

    st = provider_status()
    assert st and st[0]["name"] == "sf"
    if not st[0]["available"]:
        assert "企业资质" in st[0]["reason"]
        assert has_real_provider() is False


def test_order_tool_starts_watching(session):
    """下完单立刻开始盯物流 —— 这才是闭环。"""
    from app.agent.tools import ToolContext, execute
    from app.tasks import service

    c = Conversation(title="t", agent_type="ship")
    session.add(c)
    session.commit()
    t = service.create_task(session, "ship", "寄花瓶", {}, c.id)
    ctx = ToolContext(session=session, conversation_id=c.id, task=t)
    out, err = execute("place_shipping_order", ctx, {
        "from_place": "杭州", "to_place": "成都", "weight_kg": 2,
        "sender_name": "张三", "receiver_name": "李四",
    })
    session.commit()
    assert not err
    d = json.loads(out)
    assert d["ok"] and d["运单号"]
    assert "已自动开始监控" in d
    w = session.scalar(select(TrackingWatch).where(TrackingWatch.tracking_no == d["运单号"]))
    assert w is not None and w.status == "watching"


def test_ship_agent_has_the_new_tools():
    from app.agent.tools import get_tools

    names = {t.name for t in get_tools("ship")}
    assert "place_shipping_order" in names
    assert "watch_package" in names


def test_prompt_requires_confirming_before_ordering():
    """下单是不可逆的外部动作,提示词必须要求先跟用户确认。"""
    from app.agent.prompts import build_system_prompt

    p = build_system_prompt("ship")
    assert "确认" in p
    assert "演示" in p, "演示下单必须要求如实说明"
