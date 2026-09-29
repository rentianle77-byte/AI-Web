"""历史自愈:防止一次中断让整个会话永久性 400。

背景(测试报告 FT-09 / FT-11):
用户点「停止」或刷新页面时,后端可能已经把"模型要调工具"写进了数据库,
工具结果却还没写就断了连接。这条残缺记录之后每轮都会被回放给模型,
而模型协议要求每个 tool_call 后面必须紧跟对应的 tool_result,
于是这个会话从此每发一句话都报 400,再也用不了。

已对真实网关验证过:三种残缺形态都必然 400。
"""
import json

import pytest

from app.agent.runtime import INCOMPLETE_TOOL_RESULT, repair_tool_pairing


def user(text="你好"):
    return {"role": "user", "content": text}


def assistant_calls(*ids, text=""):
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [{"id": i, "name": "search_knowledge", "arguments": {"query": "赔偿"}} for i in ids],
    }


def tool_result(call_id, content='{"hits":[]}'):
    return {"role": "tool", "tool_call_id": call_id, "name": "search_knowledge", "content": content, "is_error": False}


def pairs_ok(history):
    """按网关的真实约束校验历史。

    实测网关拒绝的形态有三种,这里都要覆盖:
      1. tool_call 没有对应的 tool_result
      2. tool_result 找不到对应的 tool_call(孤立结果)
      3. 同一个 tool_call_id 出现多条结果(重复应答)

    还有一条「紧邻」约束:tool_result 必须紧跟在发起调用的那条
    assistant 之后,中间不能插别的角色。
    """
    pending: set[str] = set()
    seen: set[str] = set()
    answered: set[str] = set()
    prev_role = None
    for e in history:
        role = e["role"]
        if role == "assistant":
            if pending:
                return False, f"上一组工具调用未闭合:{pending}"
            for c in e.get("tool_calls") or []:
                cid = c["id"]
                if cid in seen:
                    return False, f"tool_call id 重复:{cid}"
                pending.add(cid)
                seen.add(cid)
        elif role == "tool":
            cid = e.get("tool_call_id")
            if cid not in seen:
                return False, f"孤立的工具结果:{cid}"
            if cid in answered:
                return False, f"同一个 tool_call 有多条结果:{cid}"
            if prev_role not in ("assistant", "tool"):
                return False, f"工具结果没有紧跟在发起调用的 assistant 之后:{cid}"
            answered.add(cid)
            pending.discard(cid)
        elif role == "user":
            if pending:
                return False, f"工具调用后直接跟了用户消息:{pending}"
        prev_role = role
    return (not pending), (f"历史以未闭合的工具调用收尾:{pending}" if pending else "")


# ---------------- 正常历史不该被改动 ----------------
def test_healthy_history_untouched():
    h = [user(), assistant_calls("c1"), tool_result("c1"), {"role": "assistant", "content": "答案"}]
    assert repair_tool_pairing(h) == h
    assert pairs_ok(h)[0]


def test_multiple_calls_in_one_turn_untouched():
    h = [user(), assistant_calls("c1", "c2"), tool_result("c1"), tool_result("c2")]
    assert repair_tool_pairing(h) == h


# ---------------- 三种残缺形态(都已验证会导致 400)----------------
def test_missing_result_before_next_user_message():
    """最常见:工具跑到一半用户点了停止,下一轮又发了新消息。"""
    broken = [user(), assistant_calls("c1"), user("那我该找谁")]
    ok, why = pairs_ok(broken)
    assert not ok, "这个用例本身必须是坏的"

    fixed = repair_tool_pairing(broken)
    ok, why = pairs_ok(fixed)
    assert ok, why
    filled = [e for e in fixed if e["role"] == "tool"]
    assert len(filled) == 1
    assert filled[0]["tool_call_id"] == "c1"
    assert filled[0]["is_error"] is True
    assert "中断" in filled[0]["content"]


def test_history_ending_with_unanswered_calls():
    """请求发出前最后一条就是工具调用,没有任何结果。"""
    broken = [user(), assistant_calls("c1", "c2")]
    assert not pairs_ok(broken)[0]
    fixed = repair_tool_pairing(broken)
    ok, why = pairs_ok(fixed)
    assert ok, why
    assert [e["tool_call_id"] for e in fixed if e["role"] == "tool"] == ["c1", "c2"]


def test_orphan_tool_result_dropped():
    """截断窗口把发起调用的那条切掉了,只剩下结果。"""
    broken = [user(), tool_result("c_ghost"), user("继续")]
    fixed = repair_tool_pairing(broken)
    ok, why = pairs_ok(fixed)
    assert ok, why
    assert all(e["role"] != "tool" for e in fixed)


def test_partially_answered_batch():
    """一批三个工具,只跑完第一个就断了。"""
    broken = [user(), assistant_calls("c1", "c2", "c3"), tool_result("c1"), user("继续")]
    fixed = repair_tool_pairing(broken)
    ok, why = pairs_ok(fixed)
    assert ok, why
    tools = [e for e in fixed if e["role"] == "tool"]
    assert [t["tool_call_id"] for t in tools] == ["c1", "c2", "c3"]
    assert tools[0]["content"] == '{"hits":[]}'          # 已完成的保持原样
    assert tools[1]["content"] == INCOMPLETE_TOOL_RESULT  # 未完成的补占位
    assert tools[2]["content"] == INCOMPLETE_TOOL_RESULT


def test_repeatedly_broken_conversation_recovers():
    """连着断三轮,历史里堆了多处残缺,也要能救回来。"""
    broken = [
        user("第一轮"), assistant_calls("a1"),
        user("第二轮"), assistant_calls("b1", "b2"), tool_result("b1"),
        user("第三轮"), assistant_calls("c1"),
    ]
    fixed = repair_tool_pairing(broken)
    ok, why = pairs_ok(fixed)
    assert ok, why


def test_placeholder_is_valid_json():
    """补进去的占位结果得是合法 JSON,否则模型那边解析会出问题。"""
    data = json.loads(INCOMPLETE_TOOL_RESULT)
    assert "error" in data


def test_empty_history():
    assert repair_tool_pairing([]) == []


# ---------------- 修复后的历史必须能过协议校验 ----------------
@pytest.mark.parametrize(
    "broken",
    [
        [user(), assistant_calls("c1"), user("x")],
        [user(), assistant_calls("c1", "c2")],
        [user(), tool_result("ghost")],
        [user(), assistant_calls("c1"), tool_result("c1"), assistant_calls("c2"), user("x")],
        [user(), assistant_calls("c1"), tool_result("ghost"), user("x")],
    ],
)
def test_all_broken_shapes_become_valid(broken):
    ok, why = pairs_ok(repair_tool_pairing(broken))
    assert ok, why


# ---------------- 重复调用收敛(测试报告里模型连查 7 次)----------------
from app.agent.runtime import MAX_SAME_TOOL_CALLS, _guard_repetition


def test_repetition_guard_silent_below_threshold():
    recent = ["search_knowledge"] * (MAX_SAME_TOOL_CALLS - 1)
    out = _guard_repetition("search_knowledge", recent, '{"hits":[]}')
    assert out == '{"hits":[]}', "没到阈值不该改结果"


def test_repetition_guard_injects_hint():
    recent = ["search_knowledge"] * MAX_SAME_TOOL_CALLS
    data = json.loads(_guard_repetition("search_knowledge", recent, '{"hits":[]}'))
    assert "_system_hint" in data
    assert "不要再用同义词重复检索" in data["_system_hint"]


def test_repetition_guard_counts_per_tool():
    """别的工具调得多,不该连累当前这个。"""
    recent = ["query_tracking"] * 6
    assert _guard_repetition("search_knowledge", recent, '{"hits":[]}') == '{"hits":[]}'


def test_repetition_guard_survives_non_json():
    out = _guard_repetition("search_knowledge", ["search_knowledge"] * 5, "not json at all")
    data = json.loads(out)
    assert data["result"] == "not json at all"
    assert "_system_hint" in data


def test_repetition_guard_keeps_original_fields():
    recent = ["search_knowledge"] * 4
    data = json.loads(_guard_repetition("search_knowledge", recent, '{"hits":[1,2],"query":"x"}'))
    assert data["hits"] == [1, 2] and data["query"] == "x"


def test_repetition_hint_not_persisted():
    """收敛提示带"你已经调用 N 次"这种一次性信息,不该落库。

    落库的话,下一轮回放时模型会看到一条过期的提示,反而被误导。
    正确做法:数据库存原始工具结果,提示只加在本轮发给模型的历史里。

    这条测的是实际落库内容,不是源码里的变量名 —— 变量改名不该让测试失效,
    而行为变了必须让测试失败。
    """
    import asyncio

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    import app.agent.runtime as runtime
    import app.followup.scheduler as sched
    import app.db as db_mod
    from app.db import Base
    from app.llm.base import LLMEvent, LLMClient, ToolCall
    from app.models import Conversation, Message

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    class RepeatSearcher(LLMClient):
        """连查 5 次知识库,足以越过收敛阈值。"""

        provider, model = "stub", "stub"

        def __init__(self):
            self.n = 0

        async def astream(self, system, messages, tools=None):
            self.n += 1
            if self.n <= 5:
                yield LLMEvent(type="tool_call", tool_call=ToolCall(id=f"c{self.n}", name="search_knowledge", arguments={"query": "赔偿"}))
                yield LLMEvent(type="done", stop_reason="tool_use")
            else:
                yield LLMEvent(type="text_delta", text="查完了")
                yield LLMEvent(type="done", stop_reason="end_turn")

    old_sessions = (db_mod.SessionLocal, runtime.SessionLocal, sched.SessionLocal)
    db_mod.SessionLocal = runtime.SessionLocal = sched.SessionLocal = maker
    try:
        s = maker()
        c = Conversation(title="t", agent_type="claim")
        s.add(c)
        s.commit()
        cid = c.id
        s.close()

        async def go():
            async for _ in runtime.run_turn(cid, "未保价破损赔多少", client=RepeatSearcher()):
                pass

        asyncio.run(go())

        s = maker()
        tool_msgs = list(s.scalars(select(Message).where(Message.conversation_id == cid, Message.role == "tool")))
        s.close()
        assert len(tool_msgs) >= 4, f"应该记录了多次检索,实际 {len(tool_msgs)}"
        for m in tool_msgs:
            assert "_system_hint" not in m.content, "收敛提示不该出现在落库内容里"
            assert "你已经调用" not in m.content
    finally:
        db_mod.SessionLocal, runtime.SessionLocal, sched.SessionLocal = old_sessions


# ---------------- FT-08:跟进触发提示不该在后续轮次反复生效 ----------------
def test_followup_trigger_replaced_on_replay():
    """跟进提示原文开头是「现在是系统触发的主动跟进时刻」。

    原样回放的话,之后每轮普通对话都会读到这句;跟进做过几次以后
    历史里堆着好几条,模型行为就飘了(测试报告 FT-08)。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.agent.runtime import FOLLOWUP_REPLAY_MARKER, add_message, build_llm_history
    from app.db import Base
    from app.models import Conversation

    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e)()
    c = Conversation(title="t", agent_type="claim")
    s.add(c)
    s.commit()

    add_message(s, c.id, "user", "我的快递坏了", {})
    add_message(s, c.id, "assistant", "好的", {})
    add_message(s, c.id, "user", "现在是系统触发的**主动跟进**时刻,不是用户在说话。\n请你:\n1. 先看任务当前进展……",
                {"hidden": True, "is_followup_trigger": True, "followup_id": 1})
    add_message(s, c.id, "assistant", "到点了,我帮你确认了一下", {"is_followup": True})
    add_message(s, c.id, "user", "那接下来呢", {})
    s.commit()

    h = build_llm_history(s, c.id)
    joined = " ".join(e["content"] for e in h if e["role"] == "user")
    assert "系统触发的" not in joined or FOLLOWUP_REPLAY_MARKER in joined
    assert "1. 先看任务当前进展" not in joined, "跟进指令原文不该被回放"
    assert FOLLOWUP_REPLAY_MARKER in joined, "应该留一条简短标记保住时间线"
    assert len([e for e in h if e["role"] == "user"]) == 3, "消息条数不变,只是内容换成标记"
    s.close()


# ---------------- GT-08:事件队列满了不该丢最新的 ----------------
def test_event_bus_drops_oldest_not_newest():
    """队列满时丢最旧的,保证订阅者永远能看到最新状态。"""
    import asyncio

    from app.events import EventBus

    async def run():
        bus = EventBus()
        q = bus.subscribe()
        total = q.maxsize + 20
        for i in range(total):
            bus.publish({"type": "task_update", "seq": i})
        assert bus.dropped == 20, f"应该丢 20 条,实际 {bus.dropped}"
        seen = []
        while not q.empty():
            seen.append(q.get_nowait()["seq"])
        assert seen[-1] == total - 1, "最新的那条必须还在"
        assert seen[0] == 20, "丢掉的应该是最旧的 20 条"

    asyncio.run(run())


def test_event_bus_stats_exposed():
    from app.events import EventBus

    bus = EventBus()
    st = bus.stats()
    assert set(st) == {"subscribers", "dropped", "queue_size"}
    assert st["dropped"] == 0


def test_repetition_hint_survives_truncation():
    """提示必须放在 JSON 最前面。

    _truncate 从尾部截断,提示挂在末尾的话,结果一长就第一个被切掉 ——
    而结果越长越是模型该收手的时候,守卫等于在最需要时失效。
    """
    from app.agent.runtime import MAX_SAME_TOOL_CALLS, _guard_repetition, _truncate

    big = json.dumps({"quotes": [{"x": "填充" * 60} for _ in range(80)]}, ensure_ascii=False)
    out = _truncate(_guard_repetition("compare_shipping", ["compare_shipping"] * MAX_SAME_TOOL_CALLS, big))
    assert "不要再用同义词重复检索" in out
    assert out.index("_system_hint") < 50, "提示应该在最前面"


# ---------------- 写入侧:中断时补占位结果 ----------------
# 这是线上真实故障(FT-09/FT-11)对应的第一道防线,之前完全没有测试覆盖。
def _wire(tmp_engine_holder):
    """把三处 SessionLocal 换成临时库,返回 (maker, 还原函数)。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.agent.runtime as runtime
    import app.db as db_mod
    import app.followup.scheduler as sched
    from app.db import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    tmp_engine_holder.append(engine)
    old = (db_mod.SessionLocal, runtime.SessionLocal, sched.SessionLocal)
    db_mod.SessionLocal = runtime.SessionLocal = sched.SessionLocal = maker

    def restore():
        db_mod.SessionLocal, runtime.SessionLocal, sched.SessionLocal = old

    return maker, restore


def test_client_disconnect_leaves_no_dangling_calls():
    """客户端在工具执行中途断开,库里不能留下「有调用没结果」的记录。

    这正是线上那个"点了停止,该会话从此永久 400"的成因。
    """
    import asyncio

    from sqlalchemy import select

    import app.agent.runtime as runtime
    from app.llm.base import LLMClient, LLMEvent, ToolCall
    from app.models import Conversation, Message

    holder = []
    maker, restore = _wire(holder)
    try:
        class ThreeTools(LLMClient):
            provider, model = "stub", "stub"

            async def astream(self, system, messages, tools=None):
                yield LLMEvent(type="text_delta", text="我查一下")
                for i in (1, 2, 3):
                    yield LLMEvent(type="tool_call", tool_call=ToolCall(id=f"call_{i}", name="get_current_time", arguments={}))
                yield LLMEvent(type="done", stop_reason="tool_use")

        s = maker()
        c = Conversation(title="t", agent_type="claim")
        s.add(c)
        s.commit()
        cid = c.id
        s.close()

        async def go():
            gen = runtime.run_turn(cid, "我的快递坏了", client=ThreeTools())
            async for ev in gen:
                if ev["type"] == "tool_start":
                    await gen.aclose()  # 模拟用户点「停止」
                    break

        asyncio.run(go())

        s = maker()
        msgs = list(s.scalars(select(Message).where(Message.conversation_id == cid).order_by(Message.seq)))
        s.close()

        called, answered = set(), set()
        for m in msgs:
            meta = m.meta or {}
            if m.role == "assistant":
                called.update(c["id"] for c in meta.get("tool_calls") or [])
            elif m.role == "tool":
                answered.add(meta.get("tool_call_id"))
        assert called, "应该记录了工具调用"
        assert called <= answered, f"有调用没结果:{called - answered}"
        assert any((m.meta or {}).get("interrupted") for m in msgs), "被中断的应该打上标记"
    finally:
        restore()


def test_interrupted_placeholders_keep_seq_order():
    """占位结果的 seq 必须排在发起调用那条之后,不能乱序。

    取消时 finally 可能晚执行,如果沿用外层 session 直接 add,
    seq 会插到后续消息之后,历史顺序就错了。
    """
    from sqlalchemy import select

    import app.agent.runtime as runtime
    from app.models import Conversation, Message

    holder = []
    maker, restore = _wire(holder)
    try:
        s = maker()
        c = Conversation(title="t", agent_type="claim")
        s.add(c)
        s.commit()
        cid = c.id
        runtime.add_message(s, cid, "user", "你好", {})
        runtime.add_message(s, cid, "assistant", "查一下", {"tool_calls": [{"id": "x1", "name": "get_current_time", "arguments": {}}]})
        s.commit()
        s.close()

        class FakeCall:
            name = "get_current_time"

        runtime._persist_interrupted_calls(cid, {"x1": FakeCall()})

        s = maker()
        msgs = list(s.scalars(select(Message).where(Message.conversation_id == cid).order_by(Message.seq)))
        s.close()
        assert [m.role for m in msgs] == ["user", "assistant", "tool"]
        assert [m.seq for m in msgs] == sorted(m.seq for m in msgs)
        assert msgs[-1].meta["tool_call_id"] == "x1"
    finally:
        restore()


def test_persist_interrupted_survives_broken_outer_session():
    """外层 session 处于异常状态时,补占位也必须成功。

    _persist_interrupted_calls 用独立 session 就是为了这个 ——
    沿用外层会抛 PendingRollbackError 然后被静默吞掉。
    """
    from sqlalchemy import select

    import app.agent.runtime as runtime
    from app.models import Conversation, Message

    holder = []
    maker, restore = _wire(holder)
    try:
        s = maker()
        c = Conversation(title="t", agent_type="claim")
        s.add(c)
        s.commit()
        cid = c.id
        runtime.add_message(s, cid, "user", "你好", {})
        s.commit()

        # 把外层 session 弄成脏状态
        try:
            s.execute(select(Message).where(Message.nonexistent == 1))  # noqa
        except Exception:
            pass

        class FakeCall:
            name = "search_knowledge"

        runtime._persist_interrupted_calls(cid, {"y1": FakeCall()})
        s.close()

        s2 = maker()
        tools = list(s2.scalars(select(Message).where(Message.conversation_id == cid, Message.role == "tool")))
        s2.close()
        assert len(tools) == 1 and tools[0].meta["tool_call_id"] == "y1"
    finally:
        restore()


# ---------------- 收敛护栏不能误伤写入类工具 ----------------
def test_repetition_guard_exempts_write_tools():
    """update_task / save_material 天然就该被多次调用,不能劝阻模型继续调。"""
    from app.agent.runtime import REPETITION_EXEMPT, _guard_repetition

    for name in ("update_task", "save_material", "schedule_followup", "create_task"):
        assert name in REPETITION_EXEMPT
        out = _guard_repetition(name, [name] * 9, '{"ok":true}')
        assert out == '{"ok":true}', f"{name} 不该被加提示"


def test_repetition_guard_still_applies_to_query_tools():
    from app.agent.runtime import MAX_SAME_TOOL_CALLS, _guard_repetition

    for name in ("search_knowledge", "query_tracking", "compare_shipping"):
        out = _guard_repetition(name, [name] * MAX_SAME_TOOL_CALLS, '{"hits":[]}')
        assert "_system_hint" in out, f"{name} 应该受护栏约束"
