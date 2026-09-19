"""物流查询:配置了快递100 就查真实数据,否则用可复现的演示数据。"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

import httpx

from .. import clock
from ..config import settings

CARRIER_HOTLINES = {
    "顺丰": "95338",
    "中通": "95311",
    "圆通": "95554",
    "韵达": "95546",
    "申通": "95543",
    "极兔": "956128",
    "京东": "950616",
    "EMS": "11183",
    "邮政": "11185",
    "德邦": "95353",
    "菜鸟": "95188",
}

KUAIDI100_CODES = {
    "顺丰": "shunfeng",
    "中通": "zhongtong",
    "圆通": "yuantong",
    "韵达": "yunda",
    "申通": "shentong",
    "极兔": "jtexpress",
    "京东": "jd",
    "EMS": "ems",
    "邮政": "youzhengguonei",
    "德邦": "debangkuaidi",
}


def normalize_company(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().upper()
    for key in CARRIER_HOTLINES:
        if key.upper() in n:
            return key
    aliases = {"SF": "顺丰", "ZTO": "中通", "YTO": "圆通", "YD": "韵达", "STO": "申通", "JT": "极兔", "J&T": "极兔", "JD": "京东", "DEPPON": "德邦", "POST": "邮政"}
    for alias, key in aliases.items():
        if alias in n:
            return key
    return name.strip()


def detect_company(tracking_no: str) -> str | None:
    """按单号规则猜快递公司(不保证准确)。"""
    no = (tracking_no or "").strip().upper()
    if no.startswith("SF"):
        return "顺丰"
    if no.startswith("JD") or no.startswith("JDV"):
        return "京东"
    if no.startswith("YT"):
        return "圆通"
    if no.startswith("JT"):
        return "极兔"
    if no.startswith("DPK") or no.startswith("DB"):
        return "德邦"
    if len(no) == 13 and no[0] == "E" and no.endswith("CS"):
        return "EMS"
    if no.isdigit():
        if len(no) == 12 and no.startswith(("73", "75", "78", "68", "77")):
            return "中通" if no.startswith(("73", "75", "78", "68")) else "申通"
        if len(no) == 13 and no.startswith("4"):
            return "韵达"
        if len(no) == 15 and no.startswith("9"):
            return "EMS"
    return None


_SCENARIOS = ["delivered", "in_transit", "delayed", "lost", "damaged", "exception"]


def _pick_scenario(tracking_no: str, hint: str | None) -> str:
    if hint in _SCENARIOS:
        return hint
    if hint == "normal":
        return "in_transit"
    digest = hashlib.md5(tracking_no.encode()).hexdigest()
    return _SCENARIOS[int(digest[:2], 16) % len(_SCENARIOS)]


def mock_tracking(tracking_no: str, company: str | None = None, scenario_hint: str | None = None, origin: str | None = None, destination: str | None = None) -> dict:
    company = normalize_company(company) or detect_company(tracking_no) or "中通"
    scenario = _pick_scenario(tracking_no, scenario_hint)
    rng = random.Random(tracking_no)
    origin = origin or rng.choice(["广州", "杭州", "上海", "深圳", "义乌", "成都"])
    destination = destination or rng.choice(["北京", "武汉", "西安", "南京", "长沙", "郑州"])
    now = clock.now()

    def t(days_ago: float, hour: int = 10) -> str:
        dt = (now - timedelta(days=days_ago)).replace(hour=hour, minute=rng.randint(0, 59), second=0, microsecond=0)
        return clock.fmt_local(dt)

    events: list[dict] = []
    if scenario == "delivered":
        ship_days = 4
        events = [
            {"time": t(4, 18), "location": origin, "desc": f"【{origin}】快件已被揽收"},
            {"time": t(3, 23), "location": origin, "desc": f"快件已到达【{origin}转运中心】并发出"},
            {"time": t(2, 6), "location": destination, "desc": f"快件已到达【{destination}转运中心】"},
            {"time": t(1, 8), "location": destination, "desc": f"【{destination}】派件员正在派件,电话 137****" + str(rng.randint(1000, 9999))},
            {"time": t(1, 13), "location": destination, "desc": "快件已签收,签收人:菜鸟驿站(代收)" if rng.random() < 0.5 else "快件已签收,签收人:本人"},
        ]
        status, delivered = "已签收", True
    elif scenario == "damaged":
        ship_days = 3
        events = [
            {"time": t(3, 18), "location": origin, "desc": f"【{origin}】快件已被揽收"},
            {"time": t(2, 22), "location": origin, "desc": f"快件已到达【{origin}转运中心】并发出"},
            {"time": t(1, 7), "location": destination, "desc": f"快件已到达【{destination}转运中心】,备注:外包装破损,已加固"},
            {"time": t(0, 9), "location": destination, "desc": f"【{destination}】派件员正在派件"},
            {"time": t(0, 12), "location": destination, "desc": "快件已签收,签收人:本人"},
        ]
        status, delivered = "已签收(途中有破损记录)", True
    elif scenario == "in_transit":
        ship_days = 2
        events = [
            {"time": t(2, 18), "location": origin, "desc": f"【{origin}】快件已被揽收"},
            {"time": t(1, 23), "location": origin, "desc": f"快件已到达【{origin}转运中心】并发出"},
            {"time": t(0, 7), "location": "在途", "desc": f"快件正在发往【{destination}转运中心】"},
        ]
        status, delivered = "运输中", False
    elif scenario == "delayed":
        ship_days = 6
        events = [
            {"time": t(6, 18), "location": origin, "desc": f"【{origin}】快件已被揽收"},
            {"time": t(5, 23), "location": origin, "desc": f"快件已到达【{origin}转运中心】并发出"},
            {"time": t(4, 5), "location": "中转", "desc": "快件已到达【中转集散中心】"},
        ]
        status, delivered = "运输中(物流 4 天无更新,疑似延误)", False
    elif scenario == "lost":
        ship_days = 12
        events = [
            {"time": t(12, 18), "location": origin, "desc": f"【{origin}】快件已被揽收"},
            {"time": t(11, 23), "location": origin, "desc": f"快件已到达【{origin}转运中心】并发出"},
        ]
        status, delivered = "运输中(物流 11 天无更新,已超彻底延误时限,可按丢失处理)", False
    else:  # exception
        ship_days = 4
        events = [
            {"time": t(4, 18), "location": origin, "desc": f"【{origin}】快件已被揽收"},
            {"time": t(3, 23), "location": origin, "desc": f"快件已到达【{origin}转运中心】并发出"},
            {"time": t(2, 6), "location": destination, "desc": f"快件已到达【{destination}转运中心】"},
            {"time": t(1, 9), "location": destination, "desc": "派送异常:收件人电话无法接通,快件退回网点"},
        ]
        status, delivered = "派送异常", False

    last_update_days = round((now - _parse_event_time(events[-1]["time"])).total_seconds() / 86400, 1) if events else None
    return {
        "source": "mock",
        "source_note": "演示物流数据(未配置快递100 密钥)。回答用户时要说明这是演示环境数据。",
        "tracking_no": tracking_no,
        "company": company,
        "hotline": CARRIER_HOTLINES.get(company, "见官网"),
        "status": status,
        "scenario": scenario,
        "is_delivered": delivered,
        "origin": origin,
        "destination": destination,
        "shipped_days_ago": ship_days,
        "days_since_last_update": last_update_days,
        "events": list(reversed(events)),
        "queried_at": clock.fmt_local(now),
    }


def _parse_event_time(text: str):
    local = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=clock.LOCAL_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def kuaidi100_query(tracking_no: str, company: str | None, phone: str | None = None) -> dict | None:
    """快递100 企业版实时查询。失败返回 None。"""
    if not (settings.kuaidi100_key and settings.kuaidi100_customer):
        return None
    company_norm = normalize_company(company) or detect_company(tracking_no)
    com = KUAIDI100_CODES.get(company_norm or "", "")
    param = {"com": com, "num": tracking_no, "resultv2": "4"}
    if phone:
        param["phone"] = phone
    param_str = json.dumps(param, ensure_ascii=False, separators=(",", ":"))
    sign = hashlib.md5((param_str + settings.kuaidi100_key + settings.kuaidi100_customer).encode()).hexdigest().upper()
    try:
        resp = httpx.post(
            "https://poll.kuaidi100.com/poll/query.do",
            data={"customer": settings.kuaidi100_customer, "sign": sign, "param": param_str},
            timeout=10,
        )
        data = resp.json()
    except Exception:
        return None
    if str(data.get("status")) != "200":
        return None
    state_map = {"0": "运输中", "1": "已揽收", "2": "疑难件", "3": "已签收", "4": "退签", "5": "派件中", "6": "退回", "7": "转投", "8": "清关", "14": "拒签"}
    events = [{"time": e.get("ftime") or e.get("time"), "location": e.get("areaName", ""), "desc": e.get("context", "")} for e in data.get("data", [])]
    return {
        "source": "kuaidi100",
        "tracking_no": tracking_no,
        "company": company_norm or data.get("com"),
        "hotline": CARRIER_HOTLINES.get(company_norm or "", "见官网"),
        "status": state_map.get(str(data.get("state")), str(data.get("state"))),
        "is_delivered": str(data.get("state")) == "3",
        "events": events,
        "queried_at": clock.fmt_local(clock.now()),
    }


def query_tracking(tracking_no: str, company: str | None = None, scenario_hint: str | None = None, phone: str | None = None) -> dict:
    real = kuaidi100_query(tracking_no, company, phone)
    if real:
        return real
    return mock_tracking(tracking_no, company, scenario_hint)
