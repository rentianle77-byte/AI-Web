"""对话接口:SSE 流式 + 历史读取。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .. import clock
from ..agent.runtime import message_to_dict, run_turn
from ..db import SessionLocal
from ..events import bus
from ..models import Conversation, Message
from ..tasks import service
from .schemas import ChatIn, CreateConversationIn

router = APIRouter(prefix="/api", tags=["chat"])

SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def conversation_to_dict(conv: Conversation, session=None) -> dict:
    d = {
        "id": conv.id,
        "title": conv.title,
        "agent_type": conv.agent_type,
        "task_id": conv.task_id,
        "created_at": clock.iso(conv.created_at),
        "updated_at": clock.iso(conv.updated_at),
    }
    return d


@router.post("/conversations")
def create_conversation(body: CreateConversationIn):
    session = SessionLocal()
    try:
        conv = Conversation(title=body.title or "新对话", agent_type=body.agent_type)
        session.add(conv)
        session.commit()
        return conversation_to_dict(conv)
    finally:
        session.close()


@router.get("/conversations")
def list_conversations(limit: int = 50):
    session = SessionLocal()
    try:
        rows = session.scalars(select(Conversation).order_by(Conversation.updated_at.desc()).limit(min(limit, 200)))
        return [conversation_to_dict(c) for c in rows]
    finally:
        session.close()


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    session = SessionLocal()
    try:
        conv = session.get(Conversation, conversation_id)
        if not conv:
            raise HTTPException(404, "对话不存在")
        msgs = session.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq))
        visible = [message_to_dict(m) for m in msgs if not (m.meta or {}).get("hidden")]
        task = session.get(__import__("app.models", fromlist=["Task"]).Task, conv.task_id) if conv.task_id else None
        return {
            "conversation": conversation_to_dict(conv),
            "messages": visible,
            "task": service.task_to_dict(task) if task else None,
            "followups": [service.followup_to_dict(f) for f in service.list_followups(session, task_id=task.id)] if task else [],
            "materials": [service.material_to_dict(m) for m in service.list_materials(session, conversation_id=conversation_id)],
        }
    finally:
        session.close()


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    session = SessionLocal()
    try:
        conv = session.get(Conversation, conversation_id)
        if not conv:
            raise HTTPException(404, "对话不存在")
        session.delete(conv)
        session.commit()
        return {"deleted": conversation_id}
    finally:
        session.close()


@router.post("/chat")
async def chat(body: ChatIn):
    """主对话接口,返回 SSE 事件流。"""
    session = SessionLocal()
    try:
        if body.conversation_id:
            conv = session.get(Conversation, body.conversation_id)
            if not conv:
                raise HTTPException(404, "对话不存在")
        else:
            conv = Conversation(title="新对话", agent_type=body.agent_type or "auto")
            session.add(conv)
            session.commit()
        if body.agent_type and body.agent_type != "auto":
            conv.agent_type = body.agent_type
            session.commit()
        conversation_id = conv.id
    finally:
        session.close()

    async def gen():
        try:
            async for event in run_turn(conversation_id, body.message):
                yield _sse(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"服务端异常:{type(exc).__name__}: {exc}"})
            yield _sse({"type": "done", "stop_reason": "error"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/events")
async def events_stream():
    """全局事件流:后台跟进触发时,前端靠这个实时收到主动消息。"""
    queue = bus.subscribe()

    async def gen():
        try:
            yield _sse({"type": "connected", "at": clock.fmt_local(clock.now())})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    yield _sse(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)
