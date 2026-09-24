"""Agent 运行时:多轮工具循环 + 流式输出 + 状态持久化。

一轮对话 = 反复执行 [调模型 → 模型要工具 → 执行工具 → 把结果喂回去] 直到模型给出最终答复。
过程中产生的事件实时推给前端,用户能看见"它正在查什么、算什么"。
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from sqlalchemy import func, select

from .. import clock
from ..config import settings
from ..db import SessionLocal
from ..events import bus
from ..llm.base import LLMClient, ToolCall
from ..llm.factory import get_client
from ..models import Conversation, Message, Task
from ..tasks import service
from . import tools as tools_mod
from .prompts import FOLLOWUP_PROMPT, build_system_prompt
from .router import detect_agent_type, guess_title

log = logging.getLogger(__name__)

# 喂回给模型的工具结果做长度保护,防止单条几万字撑爆上下文
MAX_TOOL_RESULT_CHARS = 6000


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + f"\n...(结果过长,已截断,原长度 {len(text)} 字符)"


# 同一个工具在一轮对话里连调多少次就该收手
MAX_SAME_TOOL_CALLS = 3


def _guard_repetition(tool_name: str, recent: list[str], result_json: str) -> str:
    """同一工具反复调用时,在结果里明说"别再查了",避免模型空转。

    真实现象:知识库检索落空时,模型会连查七八次,关键词越退化越短
    (「保价」「破损」「赔偿」「快递」),白白耗掉十几秒和大量 token。
    模型看不到自己调了几次,所以得由我们把这个事实塞回给它。
    """
    used = recent.count(tool_name)
    if used < MAX_SAME_TOOL_CALLS:
        return result_json
    hint = (
        f"【系统提示】本轮你已经调用 {tool_name} {used + 1} 次了。"
        "不要再用同义词重复检索 —— 换关键词通常也不会有新结果。"
        "请基于已拿到的信息作答;确实缺内容就如实告诉用户这一点不确定,"
        "或者换一个不同的工具。"
    )
    try:
        data = json.loads(result_json)
        if isinstance(data, dict):
            # 提示放在最前面。_truncate 是从尾部截断的,挂在末尾的话
            # 结果一长(compare_shipping 实测能到 6018 字)提示就第一个被切掉,
            # 守卫等于没加 —— 而结果越长越是模型该收手的时候。
            return json.dumps({"_system_hint": hint, **data}, ensure_ascii=False, default=str)
    except json.JSONDecodeError:
        pass
    return json.dumps({"_system_hint": hint, "result": result_json}, ensure_ascii=False)


def _next_seq(session, conversation_id: str) -> int:
    current = session.scalar(select(func.max(Message.seq)).where(Message.conversation_id == conversation_id))
    return (current or 0) + 1


def add_message(session, conversation_id: str, role: str, content: str, meta: dict | None = None) -> Message:
    msg = Message(conversation_id=conversation_id, seq=_next_seq(session, conversation_id), role=role, content=content, meta=meta or {})
    session.add(msg)
    session.flush()
    return msg


def message_to_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "seq": m.seq,
        "role": m.role,
        "content": m.content,
        "meta": m.meta or {},
        "created_at": clock.iso(m.created_at),
        "created_at_text": clock.fmt_local(m.created_at),
    }


# 回放历史时用它代替跟进触发提示的原文,避免旧指令在后续每一轮反复生效
FOLLOWUP_REPLAY_MARKER = "(系统在此处触发了一次主动跟进,下面是你当时的汇报)"

INCOMPLETE_TOOL_RESULT = json.dumps(
    {"error": "该工具调用没有完成(用户中断了上一轮回答)。如果还需要这个结果,请重新调用一次。"},
    ensure_ascii=False,
)


def repair_tool_pairing(history: list[dict]) -> list[dict]:
    """修补历史里配不上对的工具调用,防止整个会话永久性 400。

    模型协议要求每个 tool_call 后面**紧跟**它的 tool_result,少一个都会被拒绝。
    但用户点「停止」、刷新页面或关掉标签页时,后端可能已经把"模型要调工具"
    存进了数据库,工具结果却还没写就断了连接。这条残缺记录会被之后每一轮
    回放给模型,导致这个会话从此每发一句话都报 400,再也用不了。

    这里做两件事:
      1. 工具调用缺结果 → 补一条说明未完成的结果
      2. 结果找不到对应的调用 → 丢掉

    放在读取侧而不是写入侧,这样线上已经坏掉的会话也能自动恢复。
    """
    fixed: list[dict] = []
    for entry in history:
        if entry["role"] == "tool":
            # 必须紧跟在带 tool_calls 的 assistant 之后,或跟在别的 tool 结果之后
            prev = fixed[-1] if fixed else None
            if prev is None or prev["role"] not in ("assistant", "tool"):
                continue
            anchor = next((e for e in reversed(fixed) if e["role"] == "assistant"), None)
            known = {c.get("id") for c in (anchor or {}).get("tool_calls", [])}
            if entry.get("tool_call_id") not in known:
                continue  # 孤立结果,丢弃
            fixed.append(entry)
            continue

        # 轮到新消息了,先把上一组工具调用缺的结果补齐
        if fixed:
            anchor = next((e for e in reversed(fixed) if e["role"] == "assistant"), None)
            if anchor is not None and anchor.get("tool_calls"):
                anchor_idx = len(fixed) - 1 - next(i for i, e in enumerate(reversed(fixed)) if e["role"] == "assistant")
                answered = {e.get("tool_call_id") for e in fixed[anchor_idx + 1 :] if e["role"] == "tool"}
                for call in anchor["tool_calls"]:
                    if call.get("id") and call["id"] not in answered:
                        fixed.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "name": call.get("name", ""),
                                "content": INCOMPLETE_TOOL_RESULT,
                                "is_error": True,
                            }
                        )
        fixed.append(entry)

    # 历史不能以"只调工具没结果"收尾
    anchor = next((e for e in reversed(fixed) if e["role"] == "assistant"), None)
    if anchor is not None and anchor.get("tool_calls"):
        anchor_idx = len(fixed) - 1 - next(i for i, e in enumerate(reversed(fixed)) if e["role"] == "assistant")
        answered = {e.get("tool_call_id") for e in fixed[anchor_idx + 1 :] if e["role"] == "tool"}
        for call in anchor["tool_calls"]:
            if call.get("id") and call["id"] not in answered:
                fixed.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call.get("name", ""),
                        "content": INCOMPLETE_TOOL_RESULT,
                        "is_error": True,
                    }
                )
    return fixed


def build_llm_history(session, conversation_id: str, limit: int = 60) -> list[dict]:
    """把数据库里的消息还原成 LLM 需要的格式(含 tool_call / tool_result 配对)。"""
    rows = list(
        session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq.desc()).limit(limit)
        )
    )[::-1]
    history: list[dict] = []
    for m in rows:
        meta = m.meta or {}
        if m.role == "user":
            if meta.get("is_followup_trigger"):
                # 跟进触发提示是一次性的系统指令,原文很长而且开头就是
                # 「现在是系统触发的主动跟进时刻,不是用户在说话」。
                # 原样回放的话,之后每一轮普通对话都会读到这句,
                # 跟进做过几次以后历史里堆着好几条,模型行为就飘了
                # (测试报告 FT-08:「可能会受到上下文的影响」)。
                # 回放时压成一行事实陈述,保留时间线但不再重复下指令。
                history.append({"role": "user", "content": FOLLOWUP_REPLAY_MARKER})
                continue
            history.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            if meta.get("tool_calls"):
                entry["tool_calls"] = meta["tool_calls"]
            history.append(entry)
        elif m.role == "tool":
            history.append({"role": "tool", "tool_call_id": meta.get("tool_call_id", ""), "name": meta.get("name", ""), "content": m.content, "is_error": meta.get("is_error", False)})

    # 截断窗口可能把某轮的工具结果切掉一半,修补后再丢掉开头非 user 的消息
    history = repair_tool_pairing(history)
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def _load_task(session, conv: Conversation) -> Task | None:
    if conv.task_id:
        return session.get(Task, conv.task_id)
    tasks = service.list_tasks(session, conversation_id=conv.id)
    return tasks[0] if tasks else None


async def run_turn(
    conversation_id: str,
    user_text: str | None,
    *,
    is_followup: bool = False,
    followup_message: str | None = None,
    followup_id: int | None = None,
    client: LLMClient | None = None,
) -> AsyncIterator[dict]:
    """执行一轮对话,产出事件流。事件 type:
    start / text / tool_start / tool_end / task_update / material / followup / message_saved / done / error
    """
    client = client or get_client()
    session = SessionLocal()
    try:
        conv = session.get(Conversation, conversation_id)
        if conv is None:
            yield {"type": "error", "message": f"对话 {conversation_id} 不存在"}
            return

        # ---- 意图路由(仅在还没定型时)----
        if not is_followup and user_text and conv.agent_type in ("auto", "general"):
            detected = detect_agent_type(user_text)
            if detected != "general":
                conv.agent_type = detected
            if conv.title in ("新对话", "", None):
                conv.title = guess_title(user_text, detected)
        agent_type = conv.agent_type if conv.agent_type != "auto" else "general"

        # ---- 落库用户消息 / 跟进触发消息 ----
        if is_followup:
            prompt_text = FOLLOWUP_PROMPT.format(message=followup_message or "该跟进了")
            add_message(session, conv.id, "user", prompt_text, {"hidden": True, "is_followup_trigger": True, "followup_id": followup_id})
        elif user_text:
            m = add_message(session, conv.id, "user", user_text, {})
            session.commit()
            yield {"type": "message_saved", "message": message_to_dict(m)}

        task = _load_task(session, conv)
        ctx = tools_mod.ToolContext(session=session, conversation_id=conv.id, task=task)
        tool_specs = tools_mod.get_tools(agent_type)

        yield {"type": "start", "agent_type": agent_type, "provider": client.provider, "model": client.model, "conversation_id": conv.id}

        history = build_llm_history(session, conv.id)
        session.commit()

        # 记录本轮已调过的工具,用于发现"同一个工具反复查、关键词越查越短"的空转
        recent_tools: list[str] = []

        for iteration in range(settings.max_tool_iterations):
            # 每轮都用最新任务状态重建系统提示词,让模型始终知道自己走到哪了
            task_summary = service.task_summary_for_prompt(session, ctx.task) if ctx.task else None
            system = build_system_prompt(agent_type, task_summary, clock.fmt_local(clock.now()))

            text_buf: list[str] = []
            calls: list[ToolCall] = []
            stop_reason = "end_turn"
            errored = False

            try:
                async for ev in client.astream(system, history, tool_specs):
                    if ev.type == "text_delta":
                        text_buf.append(ev.text)
                        yield {"type": "text", "text": ev.text}
                    elif ev.type == "tool_call" and ev.tool_call:
                        calls.append(ev.tool_call)
                    elif ev.type == "done":
                        stop_reason = ev.stop_reason or "end_turn"
                    elif ev.type == "error":
                        errored = True
                        yield {"type": "error", "message": ev.text}
            except Exception as exc:  # noqa: BLE001
                log.exception("LLM 调用失败")
                errored = True
                yield {"type": "error", "message": f"模型调用失败:{type(exc).__name__}: {exc}"}

            if errored:
                session.commit()
                yield {"type": "done", "stop_reason": "error"}
                return

            assistant_text = "".join(text_buf)
            call_dicts = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]

            meta: dict[str, Any] = {}
            if call_dicts:
                meta["tool_calls"] = call_dicts
            if is_followup and iteration == 0:
                meta["is_followup"] = True
                meta["followup_id"] = followup_id
            saved = add_message(session, conv.id, "assistant", assistant_text, meta)
            session.commit()
            history.append({"role": "assistant", "content": assistant_text, "tool_calls": call_dicts} if call_dicts else {"role": "assistant", "content": assistant_text})
            yield {"type": "message_saved", "message": message_to_dict(saved)}

            if not calls:
                break

            # ---- 执行工具 ----
            # 客户端随时可能断开(点停止、刷页面),一旦在工具执行中途断掉,
            # 数据库里就会留下"有调用没结果"的残缺记录。这里先记下本轮待办,
            # 在 finally 里兜底补齐,读取侧的 repair_tool_pairing 是第二道防线。
            pending_calls = {c.id: c for c in calls}
            try:
                for call in calls:
                    display = tools_mod.tool_display_name(call.name)
                    yield {"type": "tool_start", "id": call.id, "name": call.name, "display": display, "arguments": call.arguments}
                    result_json, is_error = tools_mod.execute(call.name, ctx, call.arguments)
                    # 收敛提示只在本轮喂给模型,不落库 —— 它带着"你已经调用 N 次"
                    # 这种一次性信息,存进去下一轮回放时就成了误导
                    hinted = _guard_repetition(call.name, recent_tools, result_json)
                    session.commit()

                    tool_msg = add_message(session, conv.id, "tool", _truncate(result_json), {"tool_call_id": call.id, "name": call.name, "display": display, "is_error": is_error, "arguments": call.arguments})
                    session.commit()
                    pending_calls.pop(call.id, None)
                    history.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": _truncate(hinted), "is_error": is_error})
                    recent_tools.append(call.name)

                    try:
                        parsed = json.loads(result_json)
                    except json.JSONDecodeError:
                        parsed = {"raw": result_json[:500]}
                    yield {"type": "tool_end", "id": call.id, "name": call.name, "display": display, "is_error": is_error, "result": parsed, "message": message_to_dict(tool_msg)}

                    # 工具改了业务状态,单独推一条让前端面板刷新
                    if call.name in ("create_task", "update_task"):
                        if ctx.task:
                            yield {"type": "task_update", "task": service.task_to_dict(ctx.task)}
                    elif call.name == "save_material" and isinstance(parsed, dict) and parsed.get("id"):
                        yield {"type": "material", "material": parsed}
                    elif call.name in ("schedule_followup", "cancel_followups") and ctx.task:
                        yield {"type": "followup", "followups": [service.followup_to_dict(f) for f in service.list_followups(session, task_id=ctx.task.id)]}
            finally:
                # 中途断开时把没跑完的工具补一条结果,不让残缺记录留在库里
                if pending_calls:
                    try:
                        for cid, call in pending_calls.items():
                            add_message(
                                session, conv.id, "tool", INCOMPLETE_TOOL_RESULT,
                                {"tool_call_id": cid, "name": call.name, "display": tools_mod.tool_display_name(call.name), "is_error": True, "interrupted": True},
                            )
                        session.commit()
                        log.info("对话 %s 有 %d 个工具调用被中断,已补占位结果", conv.id, len(pending_calls))
                    except Exception:  # noqa: BLE001
                        log.exception("补占位工具结果失败")
        else:
            yield {"type": "error", "message": f"工具调用超过 {settings.max_tool_iterations} 轮仍未收敛,已停止。"}

        # ---- 收尾 ----
        if is_followup and followup_id:
            from ..models import Followup

            f = session.get(Followup, followup_id)
            if f and f.status == "fired":
                f.status = "acked"
        conv.updated_at = clock.now()
        session.commit()

        payload: dict[str, Any] = {"type": "done", "stop_reason": "end_turn"}
        if ctx.task:
            payload["task"] = service.task_to_dict(ctx.task)
            payload["followups"] = [service.followup_to_dict(f) for f in service.list_followups(session, task_id=ctx.task.id)]
            payload["materials"] = [service.material_to_dict(m) for m in service.list_materials(session, task_id=ctx.task.id)]
        yield payload
    finally:
        session.close()


async def run_followup(followup_id: int) -> list[dict]:
    """后台调度器触发的跟进:跑完一轮并把结果广播给所有前端连接。"""
    from ..models import Followup

    session = SessionLocal()
    try:
        f = session.get(Followup, followup_id)
        if not f or f.status != "scheduled":
            return []
        f.status = "fired"
        f.fired_at = clock.now()
        conversation_id = f.conversation_id
        message = f.message
        session.commit()
    finally:
        session.close()

    if not conversation_id:
        return []

    events: list[dict] = []
    bus.publish({"type": "followup_fired", "followup_id": followup_id, "conversation_id": conversation_id, "message": message})
    async for ev in run_turn(conversation_id, None, is_followup=True, followup_message=message, followup_id=followup_id):
        events.append(ev)
        if ev["type"] in ("message_saved", "task_update", "material", "followup", "error"):
            bus.publish({**ev, "conversation_id": conversation_id, "from_followup": True})
    bus.publish({"type": "followup_done", "followup_id": followup_id, "conversation_id": conversation_id})
    return events
