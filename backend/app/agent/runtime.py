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
            history.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            if meta.get("tool_calls"):
                entry["tool_calls"] = meta["tool_calls"]
            history.append(entry)
        elif m.role == "tool":
            history.append({"role": "tool", "tool_call_id": meta.get("tool_call_id", ""), "name": meta.get("name", ""), "content": m.content, "is_error": meta.get("is_error", False)})
    # 首条必须是 user
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
            for call in calls:
                display = tools_mod.tool_display_name(call.name)
                yield {"type": "tool_start", "id": call.id, "name": call.name, "display": display, "arguments": call.arguments}
                result_json, is_error = tools_mod.execute(call.name, ctx, call.arguments)
                session.commit()

                tool_msg = add_message(session, conv.id, "tool", _truncate(result_json), {"tool_call_id": call.id, "name": call.name, "display": display, "is_error": is_error, "arguments": call.arguments})
                session.commit()
                history.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": _truncate(result_json), "is_error": is_error})

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
