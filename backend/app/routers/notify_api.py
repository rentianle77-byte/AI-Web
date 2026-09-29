"""通知设置与自检。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..notify import available_channels, send_notification
from ..notify.service import get_contact, set_contact

router = APIRouter(prefix="/api/notify", tags=["notify"])


class ContactIn(BaseModel):
    contact: str = Field(max_length=200, description="收件邮箱")


@router.get("/status")
def status():
    chans = available_channels()
    return {
        "contact": get_contact(),
        "channels": chans,
        "ready": any(c["available"] for c in chans),
    }


@router.put("/contact")
def update_contact(body: ContactIn):
    value = body.contact.strip()
    if value and "@" not in value:
        raise HTTPException(400, "看起来不像邮箱地址")
    return {"contact": set_contact(value)}


@router.post("/test")
def send_test():
    """发一封测试通知,确认配置真的能用。

    光看「配置齐了」不够 —— SMTP 密码填成登录密码而不是授权码
    是最常见的错误,只有真发一次才知道。
    """
    r = send_notification(
        "【快递管家】通知测试",
        "这是一封测试通知。\n\n你能看到这条,说明跟进到点时系统能找到你了。",
        '<div style="font-family:-apple-system,\'PingFang SC\',sans-serif;line-height:1.7">'
        '<div style="font-size:15px;font-weight:600">📦 快递管家 · 通知测试</div>'
        '<p>你能看到这条,说明跟进到点时系统能找到你了。</p></div>',
    )
    if not r.sent:
        raise HTTPException(400, r.summary())
    return {"ok": True, "summary": r.summary()}
