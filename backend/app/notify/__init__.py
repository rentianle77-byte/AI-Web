"""通知触达。

产品的核心卖点是「到点主动找你」,但在这之前,跟进触发后用户
必须自己打开网页才看得见 —— 那不叫主动。

这个模块把跟进推到用户手机上。设计成多通道:
邮件不需要任何资质审批,配个 SMTP 就能用;
微信模板消息和短信需要企业主体,拿到资质后填配置即可启用,代码不用改。
"""
from .service import (  # noqa: F401
    NotifyResult,
    available_channels,
    notify_followup,
    send_notification,
)
