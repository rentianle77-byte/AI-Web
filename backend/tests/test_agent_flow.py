"""端到端:用演示模型把三条 Agent 流程从头跑一遍,确认工具链、状态机、跟进、材料都落到位。"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
from app.db import Base


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """把全局 SessionLocal 换成临时库,避免污染开发数据。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    import app.agent.runtime as runtime
    import app.tasks.service as service
    import app.followup.scheduler as sched

    monkeypatch.setattr(runtime, "SessionLocal", maker)
    monkeypatch.setattr(sched, "SessionLocal", maker)
    from app.llm.mock import MockClient

    return maker, MockClient()


def run_turn(maker, client, conv_id, text):
    from app.agent.runtime import run_turn as rt

    async def go():
        return [ev async for ev in rt(conv_id, text, client=client)]

    return asyncio.run(go())


def new_conv(maker, agent_type="auto"):
    from app.models import Conversation

    s = maker()
    c = Conversation(title="新对话", agent_type=agent_type)
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def tool_names(events):
    return [e["name"] for e in events if e["type"] == "tool_end"]


def errors(events):
    return [e for e in events if e["type"] == "error"] + [e for e in events if e["type"] == "tool_end" and e["is_error"]]


@pytest.mark.parametrize(
    "text,expect_agent,expect_tools",
    [
        ("我在淘宝买的玻璃杯 780 块,中通寄来碎了,单号 73121234567,没保价。", "claim", {"create_task", "query_tracking", "assess_claim"}),
        ("三天前天猫买的运动鞋不合脚,吊牌还在,有运费险,帮我退掉。", "return", {"create_task", "check_return_eligibility"}),
        ("我要从杭州寄个 2 公斤的花瓶到成都,哪家划算?", "ship", {"create_task", "compare_shipping"}),
    ],
)
def test_each_agent_runs_its_toolchain(wired, text, expect_agent, expect_tools):
    maker, client = wired
    cid = new_conv(maker)
    events = run_turn(maker, client, cid, text)

    start = next(e for e in events if e["type"] == "start")
    assert start["agent_type"] == expect_agent
    assert expect_tools <= set(tool_names(events)), f"缺少工具:{expect_tools - set(tool_names(events))}"
    assert not errors(events), errors(events)

    done = events[-1]
    assert done["type"] == "done"
    assert done["task"]["type"] == expect_agent
    assert done["task"]["progress"] > 0, "任务进度没有推进"
    assert done["followups"], "没有安排任何主动跟进"
    assert done["materials"], "没有生成任何材料"


def test_followup_fires_after_clock_advance(wired):
    """核心卖点:到点系统自己开口,用户不用回来问。"""
    from app import clock
    from app.followup.scheduler import due_followup_ids
    from app.models import Message

    maker, client = wired
    clock.set_offset(0)
    cid = new_conv(maker)
    run_turn(maker, client, cid, "我在淘宝买的玻璃杯 780 块,中通寄来碎了,单号 73121234567,没保价。")

    assert due_followup_ids() == [], "跟进不该立刻到期"
    clock.advance(73)
    due = due_followup_ids()
    assert len(due) == 1

    import app.agent.runtime as runtime

    async def go():
        return await runtime.run_followup(due[0])

    events = asyncio.run(go())
    assert events, "跟进没有产生任何事件"

    s = maker()
    followup_msgs = [m for m in s.query(Message).filter(Message.role == "assistant") if (m.meta or {}).get("is_followup")]
    s.close()
    assert followup_msgs, "对话里没有出现系统主动发出的消息"
    clock.set_offset(0)


def test_multi_turn_keeps_same_task(wired):
    maker, client = wired
    cid = new_conv(maker)
    first = run_turn(maker, client, cid, "我在淘宝买的玻璃杯 780 块,中通寄来碎了,单号 73121234567,没保价。")
    task_id = first[-1]["task"]["id"]
    second = run_turn(maker, client, cid, "我已经找客服报损了,他们说只能赔三倍运费。")
    assert second[-1]["task"]["id"] == task_id, "第二轮不应该新建任务"


def test_tool_error_is_reported_not_crashed(wired):
    """工具报错时要把错误喂回模型,而不是整轮崩掉。"""
    from app.agent.tools import ToolContext, execute
    from app.models import Conversation

    maker, _ = wired
    s = maker()
    c = s.get(Conversation, new_conv(maker))
    ctx = ToolContext(session=s, conversation_id=c.id, task=None)
    out, is_err = execute("update_task", ctx, {"step_key": "x", "step_status": "done"})
    s.close()
    assert is_err is True
    assert "任务" in out
