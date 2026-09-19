from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateConversationIn(BaseModel):
    title: str | None = None
    agent_type: Literal["auto", "claim", "return", "ship", "general"] = "auto"


class ChatIn(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)
    agent_type: Literal["auto", "claim", "return", "ship", "general"] | None = None


class CreateTaskIn(BaseModel):
    task_type: Literal["claim", "return", "ship"]
    title: str
    details: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None


class UpdateTaskIn(BaseModel):
    step_key: str | None = None
    step_status: Literal["pending", "in_progress", "done", "skipped"] | None = None
    note: str | None = None
    details: dict[str, Any] | None = None
    status: Literal["active", "waiting", "attention", "done", "cancelled"] | None = None
    title: str | None = None


class FollowupIn(BaseModel):
    task_id: str
    delay_hours: float = Field(gt=0, le=24 * 365)
    message: str
    kind: Literal["check", "remind", "escalate"] = "check"
    conversation_id: str | None = None


class MaterialIn(BaseModel):
    title: str
    kind: Literal["script", "letter", "checklist", "guide", "complaint", "other"] = "other"
    content: str
    task_id: str | None = None
    conversation_id: str | None = None


class AdvanceClockIn(BaseModel):
    hours: float = Field(description="快进多少小时,可为负数回退")


class ShippingIn(BaseModel):
    from_place: str
    to_place: str
    weight_kg: float = Field(gt=0, le=1000)
    length_cm: float | None = None
    width_cm: float | None = None
    height_cm: float | None = None
    item_type: str | None = None
    urgency: Literal["normal", "fast", "cheapest"] = "normal"
    declared_value: float | None = None
