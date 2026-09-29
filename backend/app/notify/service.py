"""通知调度:决定发给谁、发什么、走哪个通道。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import clock
from ..config import settings
from ..db import SessionLocal
from ..models import Setting
from .channels import ALL_CHANNELS, SendResult

log = logging.getLogger(__name__)

# 收件人存在 settings 表里,可以在界面上改,不用重启服务
CONTACT_KEY = "notify_contact"


def get_contact() -> str:
    """用户的收件地址。界面上设置的优先,其次用配置文件里的默认值。"""
    session = SessionLocal()
    try:
        row = session.get(Setting, CONTACT_KEY)
        if row and row.value.strip():
            return row.value.strip()
    except Exception:  # noqa: BLE001  —— 表还没建好时不要影响主流程
        log.debug("读取通知联系方式失败", exc_info=True)
    finally:
        session.close()
    return settings.notify_default_contact or ""


def set_contact(value: str) -> str:
    session = SessionLocal()
    try:
        row = session.get(Setting, CONTACT_KEY)
        if row:
            row.value = value.strip()
        else:
            session.add(Setting(key=CONTACT_KEY, value=value.strip()))
        session.commit()
        return value.strip()
    finally:
        session.close()


@dataclass
class NotifyResult:
    sent: bool = False
    results: list[SendResult] = field(default_factory=list)
    skipped_reason: str = ""

    def summary(self) -> str:
        if self.skipped_reason:
            return self.skipped_reason
        ok = [r.channel for r in self.results if r.ok]
        bad = [f"{r.channel}({r.detail})" for r in self.results if not r.ok]
        parts = []
        if ok:
            parts.append("已送达:" + "、".join(ok))
        if bad:
            parts.append("失败:" + "、".join(bad))
        return " / ".join(parts) or "没有可用通道"


def available_channels() -> list[dict]:
    """哪些通道能用,不能用的缺什么 —— 给设置页面显示。"""
    out = []
    for ch in ALL_CHANNELS:
        ok = ch.available()
        out.append({
            "name": ch.name,
            "label": ch.label,
            "available": ok,
            "reason": "" if ok else ch.why_unavailable(),
        })
    return out


def send_notification(subject: str, body_text: str, body_html: str | None = None, to: str | None = None) -> NotifyResult:
    """往所有可用通道发一份。

    故意不做"发成功一个就停":用户可能同时配了邮件和微信,
    两边都收到比只收一边更符合预期 —— 这是提醒,不是消息队列。
    """
    contact = (to or get_contact()).strip()
    usable = [ch for ch in ALL_CHANNELS if ch.available()]
    if not usable:
        return NotifyResult(skipped_reason="没有配置任何通知通道(见 backend/.env 的 SMTP_* / SERVERCHAN_KEY)")
    # 邮件需要收件地址,webhook 和 Server 酱不需要
    if not contact and all(ch.name == "email" for ch in usable):
        return NotifyResult(skipped_reason="还没设置收件邮箱(界面右上角「通知设置」或 .env 的 NOTIFY_DEFAULT_CONTACT)")

    results = []
    for ch in usable:
        try:
            results.append(ch.send(contact, subject, body_text, body_html))
        except Exception as exc:  # noqa: BLE001  —— 一个通道挂了不能影响别的
            log.exception("通道 %s 发送异常", ch.name)
            results.append(SendResult(False, ch.name, f"{type(exc).__name__}: {exc}"))
    return NotifyResult(sent=any(r.ok for r in results), results=results)


def _plain(text: str) -> str:
    """把 Markdown 里的标记去掉,邮件纯文本部分用。"""
    out = []
    for line in (text or "").splitlines():
        line = line.replace("**", "").replace("## ", "").replace("### ", "").replace("# ", "")
        out.append(line)
    return "\n".join(out)


def notify_followup(task_title: str, task_id: str, message: str, agent_reply: str, conversation_id: str, base_url: str | None = None) -> NotifyResult:
    """跟进到点时通知用户。

    这是整个模块存在的理由:没有它,「主动跟进」只有用户
    恰好开着网页才看得见,卖点是虚的。
    """
    url = (base_url or settings.public_base_url or "").rstrip("/")
    link = f"{url}/?c={conversation_id}" if url else ""
    when = clock.fmt_local(clock.now())

    subject = f"【快递管家】{task_title}"
    body_lines = [
        f"到点了,我帮你看了一下「{task_title}」({task_id})。",
        "",
        _plain(agent_reply).strip() or _plain(message).strip(),
        "",
        "—" * 20,
        f"提醒时间:{when}",
    ]
    if link:
        body_lines.append(f"打开对话继续:{link}")
    body_text = "\n".join(body_lines)

    reply_html = (_plain(agent_reply).strip() or _plain(message).strip()).replace("\n", "<br>")
    body_html = f"""<div style="font-family:-apple-system,'PingFang SC',sans-serif;line-height:1.7;color:#1a1e27;max-width:640px">
  <div style="font-size:15px;font-weight:600;margin-bottom:4px">📦 快递管家</div>
  <div style="color:#5b6472;font-size:13px;margin-bottom:16px">{task_title} · {task_id}</div>
  <div style="background:#f6f7f9;border-radius:10px;padding:14px 16px;font-size:14px">{reply_html}</div>
  <div style="color:#8a93a3;font-size:12px;margin-top:16px">
    提醒时间:{when}{f'<br><a href="{link}" style="color:#4f8cff">打开对话继续</a>' if link else ''}
  </div>
</div>"""

    result = send_notification(subject, body_text, body_html)
    log.info("跟进通知 %s:%s", task_id, result.summary())
    return result
