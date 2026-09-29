"""寄件下单:让「寄件决策 Agent」真的把单下出去。

这是三条流程里唯一能做到端到端闭环的 —— 电商售后没有买家侧接口,
但寄件有:顺丰丰桥、京东物流开放平台都提供正式的下单和预约取件接口。

用户说一句「我要从杭州寄个花瓶到成都」,系统比价 → 下单 → 约上门时间
→ 返回运单号 → 自动开始盯物流。全程用户只说了一句话。

需要企业资质注册开放平台账号。没配置时走演示实现,流程照样完整,
返回的运单号带 DEMO 前缀并明确标注,不会让人误以为真下了单。
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from .. import clock
from ..config import settings

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    ok: bool
    provider: str
    is_demo: bool
    tracking_no: str = ""
    order_id: str = ""
    pickup_window: str = ""
    estimated_fee: float | None = None
    message: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "ok": self.ok,
            "承运商": self.provider,
            "运单号": self.tracking_no,
            "订单号": self.order_id,
            "上门取件时间": self.pickup_window,
            "预估费用": self.estimated_fee,
            "说明": self.message,
        }
        if self.is_demo:
            d["⚠ 演示"] = (
                "这是演示下单,没有真实发出。未配置快递公司开放平台密钥时走这条路径。"
                "告诉用户时必须说明这一点,不要让他以为快递员真的会上门。"
            )
        return d


class Provider:
    name = "base"
    label = "承运商"

    def available(self) -> bool:
        return False

    def why_unavailable(self) -> str:
        return "未配置"

    def place(self, **kw) -> OrderResult:
        raise NotImplementedError


class SFProvider:
    """顺丰丰桥开放平台。

    需要企业资质注册,拿到 partner_id(顾客编码)和 checkword(校验码)。
    签名规则:对 (报文 + timestamp + checkword) 做 MD5 再 Base64。
    """

    name = "sf"
    label = "顺丰"

    def available(self) -> bool:
        return bool(settings.sf_partner_id and settings.sf_checkword)

    def why_unavailable(self) -> str:
        return "缺少配置:SF_PARTNER_ID / SF_CHECKWORD(需企业资质到丰桥开放平台申请)"

    def _sign(self, body: str, timestamp: str) -> str:
        raw = f"{body}{timestamp}{settings.sf_checkword}"
        import urllib.parse

        quoted = urllib.parse.quote_plus(raw)
        return b64encode(hashlib.md5(quoted.encode("utf-8")).digest()).decode()

    def place(self, from_place: str, to_place: str, weight_kg: float, item_type: str,
              sender: dict, receiver: dict, declared_value: float | None = None,
              pickup_at: str | None = None, **_) -> OrderResult:
        if not self.available():
            return OrderResult(False, self.label, False, message=self.why_unavailable())

        order_id = "SF" + secrets.token_hex(8).upper()
        payload = {
            "language": "zh-CN",
            "orderId": order_id,
            "cargoDetails": [{"name": item_type or "物品", "weight": float(weight_kg)}],
            "contactInfoList": [
                {"contactType": 1, "contact": sender.get("name", ""), "mobile": sender.get("phone", ""), "address": sender.get("address", from_place)},
                {"contactType": 2, "contact": receiver.get("name", ""), "mobile": receiver.get("phone", ""), "address": receiver.get("address", to_place)},
            ],
            "monthlyCard": settings.sf_monthly_card or "",
            "payMethod": 1,
            "expressTypeId": 1,
            "isDocall": 1,  # 需要上门取件
        }
        if declared_value:
            payload["cargoDetails"][0]["amount"] = float(declared_value)
        if pickup_at:
            payload["sendStartTm"] = pickup_at

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ts = str(int(clock.now().timestamp() * 1000))
        form = {
            "partnerID": settings.sf_partner_id,
            "requestID": secrets.token_hex(16),
            "serviceCode": "EXP_RECE_CREATE_ORDER",
            "timestamp": ts,
            "msgDigest": self._sign(body, ts),
            "msgData": body,
        }
        try:
            r = httpx.post(settings.sf_base_url.rstrip("/") + "/std/service", data=form, timeout=25)
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            return OrderResult(False, self.label, False, message=f"顺丰接口调用失败:{type(exc).__name__}: {exc}")

        if str(data.get("apiResultCode")) != "A1000":
            return OrderResult(False, self.label, False, message=f"顺丰下单失败:{data.get('apiErrorMsg') or data}", raw=data)
        try:
            inner = json.loads(data["apiResultData"])
            waybill = inner["msgData"]["waybillNoInfoList"][0]["waybillNo"]
        except Exception:  # noqa: BLE001
            return OrderResult(False, self.label, False, message="顺丰返回格式看不懂,请检查接口版本", raw=data)
        return OrderResult(
            True, self.label, False,
            tracking_no=waybill, order_id=order_id,
            pickup_window=pickup_at or "已提交取件请求,快递员会联系你",
            message="已下单,快递员会上门取件",
            raw=data,
        )


class DemoProvider:
    """演示实现。

    没有企业资质时走这条路。流程完整 —— 能拿到运单号、能接着盯物流、
    能推进任务状态 —— 但运单号带 DEMO 前缀,并在返回里明确标注,
    不会让人误以为真的下了单。
    """

    name = "demo"
    label = "演示承运商"

    def available(self) -> bool:
        return True

    def place(self, from_place: str, to_place: str, weight_kg: float, item_type: str = "",
              declared_value: float | None = None, pickup_at: str | None = None,
              carrier_hint: str | None = None, **_) -> OrderResult:
        from ..domain.shipping import estimate

        quote = estimate(from_place, to_place, weight_kg, item_type=item_type, declared_value=declared_value)
        rec = quote.get("recommended") or {}
        start = clock.now_local() + timedelta(hours=2)
        window = f"{start.strftime('%m月%d日 %H:00')}-{(start + timedelta(hours=2)).strftime('%H:00')}"
        return OrderResult(
            True, carrier_hint or rec.get("short") or "平台寄件", True,
            tracking_no="DEMO" + secrets.token_hex(6).upper(),
            order_id="D" + secrets.token_hex(6).upper(),
            pickup_window=window,
            estimated_fee=rec.get("total"),
            message=f"已按「{rec.get('short') or '推荐方案'}」下单,预估 {rec.get('total')} 元,{rec.get('eta') or ''}",
        )


_PROVIDERS = [SFProvider(), DemoProvider()]


def provider_status() -> list[dict]:
    out = []
    for p in _PROVIDERS:
        if p.name == "demo":
            continue
        out.append({"name": p.name, "label": p.label, "available": p.available(), "reason": "" if p.available() else p.why_unavailable()})
    return out


def has_real_provider() -> bool:
    return any(p.available() for p in _PROVIDERS if p.name != "demo")


def place_order(**kw) -> OrderResult:
    """下单。有真实承运商就用真的,否则走演示实现。"""
    for p in _PROVIDERS:
        if p.name == "demo":
            continue
        if p.available():
            result = p.place(**kw)
            if result.ok:
                return result
            log.warning("%s 下单失败,不自动降级到演示:%s", p.label, result.message)
            return result  # 真实渠道失败就如实报错,不要假装成功
    return DemoProvider().place(**kw)
