"""通知通道。每个通道自己判断配置齐不齐,没配齐就报告不可用,不抛异常。"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx

from ..config import settings

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    ok: bool
    channel: str
    detail: str = ""


class Channel:
    name = "base"
    label = "通道"

    def available(self) -> bool:
        raise NotImplementedError

    def why_unavailable(self) -> str:
        return "未配置"

    def send(self, to: str, subject: str, body_text: str, body_html: str | None = None) -> SendResult:
        raise NotImplementedError


class EmailChannel(Channel):
    """SMTP 邮件。

    选它做第一个通道是因为零门槛:QQ 邮箱、Gmail、163 都能开 SMTP 授权码,
    不需要企业主体、不需要审核。微信模板消息和短信都要企业资质。
    """

    name = "email"
    label = "邮件"

    def available(self) -> bool:
        return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)

    def why_unavailable(self) -> str:
        missing = [k for k, v in (("SMTP_HOST", settings.smtp_host), ("SMTP_USER", settings.smtp_user), ("SMTP_PASSWORD", settings.smtp_password)) if not v]
        return f"缺少配置:{', '.join(missing)}"

    def send(self, to: str, subject: str, body_text: str, body_html: str | None = None) -> SendResult:
        if not self.available():
            return SendResult(False, self.name, self.why_unavailable())
        if not to:
            return SendResult(False, self.name, "没有收件地址")

        msg = MIMEText(body_html or body_text, "html" if body_html else "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(settings.smtp_from_name, "utf-8")), settings.smtp_user))
        msg["To"] = to

        try:
            if settings.smtp_ssl:
                # 465 端口直接建 SSL 连接(QQ 邮箱、163 都用这个)
                with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ssl.create_default_context()) as s:
                    s.login(settings.smtp_user, settings.smtp_password)
                    s.sendmail(settings.smtp_user, [to], msg.as_string())
            else:
                # 587 端口先明文连接再升级到 TLS(Gmail、Outlook)
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(settings.smtp_user, settings.smtp_password)
                    s.sendmail(settings.smtp_user, [to], msg.as_string())
        except smtplib.SMTPAuthenticationError:
            return SendResult(False, self.name, "SMTP 认证失败 —— 注意多数邮箱要用「授权码」而不是登录密码")
        except Exception as exc:  # noqa: BLE001
            return SendResult(False, self.name, f"{type(exc).__name__}: {exc}")
        return SendResult(True, self.name, f"已发送至 {to}")


class ServerChanChannel(Channel):
    """Server 酱:把消息推到微信。

    个人开发者能用,不需要企业主体 —— 这是微信触达里门槛最低的一条路。
    微信官方的模板消息要公众号 + 企业认证,那个等有资质了再接。
    """

    name = "wechat"
    label = "微信(Server 酱)"

    def available(self) -> bool:
        return bool(settings.serverchan_key)

    def why_unavailable(self) -> str:
        return "缺少配置:SERVERCHAN_KEY(到 sct.ftqq.com 用微信扫码即可获取)"

    def send(self, to: str, subject: str, body_text: str, body_html: str | None = None) -> SendResult:
        if not self.available():
            return SendResult(False, self.name, self.why_unavailable())
        try:
            r = httpx.post(
                f"https://sctapi.ftqq.com/{settings.serverchan_key}.send",
                data={"title": subject[:32], "desp": body_text[:8000]},
                timeout=15,
            )
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult(False, self.name, f"{type(exc).__name__}: {exc}")
        if data.get("code") == 0:
            return SendResult(True, self.name, "已推送到微信")
        return SendResult(False, self.name, f"推送失败:{data.get('message') or data}")


class WebhookChannel(Channel):
    """通用 webhook。企业微信机器人、飞书机器人、钉钉机器人都能接。"""

    name = "webhook"
    label = "Webhook"

    def available(self) -> bool:
        return bool(settings.notify_webhook_url)

    def send(self, to: str, subject: str, body_text: str, body_html: str | None = None) -> SendResult:
        if not self.available():
            return SendResult(False, self.name, "缺少配置:NOTIFY_WEBHOOK_URL")
        try:
            # 企业微信 / 飞书机器人都认这个形状
            r = httpx.post(
                settings.notify_webhook_url,
                json={"msgtype": "text", "text": {"content": f"{subject}\n\n{body_text}"}},
                timeout=15,
            )
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return SendResult(False, self.name, f"{type(exc).__name__}: {exc}")
        return SendResult(True, self.name, "已投递")


ALL_CHANNELS: list[Channel] = [EmailChannel(), ServerChanChannel(), WebhookChannel()]
