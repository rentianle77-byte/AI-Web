"""数据模型:对话 / 消息 / 任务 / 跟进提醒 / 材料 / 设置。"""
from __future__ import annotations

import secrets
import string
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import clock
from .db import Base

LongText = Text().with_variant(LONGTEXT, "mysql")
_ALPHABET = string.ascii_uppercase + string.digits


def new_conversation_id() -> str:
    return "c_" + secrets.token_hex(6)


def new_task_id() -> str:
    return "T-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_conversation_id)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    # auto | claim | return | ship | general
    agent_type: Mapped[str] = mapped_column(String(16), default="auto")
    task_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now, onupdate=clock.now)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.seq"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(32), ForeignKey("conversations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # user | assistant | tool
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(LongText, default="")
    # tool_calls / tool_call_id / name / raw_content / is_followup / followup_id ...
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_task_id)
    # claim | return | ship
    type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    # active | waiting | attention | done | cancelled
    status: Mapped[str] = mapped_column(String(16), default="active")
    current_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now, onupdate=clock.now)


class Followup(Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(16), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    message: Mapped[str] = mapped_column(Text)
    # check(询问进展) | remind(提醒用户做事) | escalate(升级)
    kind: Mapped[str] = mapped_column(String(16), default="check")
    # scheduled | fired | acked | cancelled
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    # script(话术) | letter(申请书/投诉信) | checklist(清单) | guide(指引) | complaint(申诉材料) | other
    kind: Mapped[str] = mapped_column(String(16), default="other")
    content: Mapped[str] = mapped_column(LongText)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
