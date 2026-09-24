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
    """按模型协议校验:每个 tool_call 都有结果,每个结果都有来源。"""
    pending: set[str] = set()
    seen: set[str] = set()
    for e in history:
        if e["role"] == "assistant":
            if pending:
                return False, f"上一组工具调用未闭合:{pending}"
            for c in e.get("tool_calls") or []:
                pending.add(c["id"])
                seen.add(c["id"])
        elif e["role"] == "tool":
            cid = e.get("tool_call_id")
            if cid not in seen:
                return False, f"孤立的工具结果:{cid}"
            pending.discard(cid)
        elif e["role"] == "user":
            if pending:
                return False, f"工具调用后直接跟了用户消息:{pending}"
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
