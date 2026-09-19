"""寄件决策引擎:参考价格表 + 体积重 + 区域 + 保价费 → 多方案对比与推荐。

价格为 2025 年前后公开渠道常见的“参考价”,各地网点、平台优惠差异很大,
最终以下单页面报价为准;引擎的价值在于“怎么算、为什么推荐”,而不是精确到分。
"""
from __future__ import annotations

import math

from .geo import ZONE_LABEL, resolve_place, zone_between

# (首重价, 续重价/kg) 按区域;first_kg 为首重公斤数
CARRIERS: list[dict] = [
    {
        "code": "platform",
        "name": "菜鸟裹裹 / 微信寄件 / 支付宝寄件(平台折扣价,走通达系或极兔)",
        "short": "平台寄件",
        "first_kg": 1,
        "divisor": 8000,
        "price": {"same_city": (5, 1), "same_province": (6, 1.5), "cross_province": (8, 3), "remote": (16, 12)},
        "eta": {"same_city": "1 天", "same_province": "1-2 天", "cross_province": "2-4 天", "remote": "5-8 天"},
        "eta_days": {"same_city": 1, "same_province": 1.5, "cross_province": 3, "remote": 6.5},
        "insurance": "flat",
        "max_kg": 30,
        "tags": ["最便宜", "上门取件", "普通物品"],
        "app": "菜鸟裹裹 APP / 微信「服务-寄快递」/ 支付宝搜「寄快递」",
        "risk": "服务由具体承运网点决定,贵重、易碎、生鲜不推荐",
    },
    {
        "code": "tongda",
        "name": "中通 / 圆通 / 韵达 / 申通 / 极兔(网点直接寄)",
        "short": "通达系",
        "first_kg": 1,
        "divisor": 8000,
        "price": {"same_city": (6, 1), "same_province": (8, 2), "cross_province": (10, 4), "remote": (18, 15)},
        "eta": {"same_city": "1 天", "same_province": "1-2 天", "cross_province": "2-4 天", "remote": "5-8 天"},
        "eta_days": {"same_city": 1, "same_province": 1.5, "cross_province": 3, "remote": 6.5},
        "insurance": "flat",
        "max_kg": 50,
        "tags": ["便宜", "普通物品"],
        "app": "各家官方小程序(中通快递 / 圆通速递 / 韵达快递 / 申通快递 / 极兔速递)",
        "risk": "网点报价随口喊,多问一家能砍价;贵重物品务必保价",
    },
    {
        "code": "jd",
        "name": "京东快递(标快)",
        "short": "京东快递",
        "first_kg": 1,
        "divisor": 6000,
        "price": {"same_city": (12, 3), "same_province": (12, 4), "cross_province": (18, 8), "remote": (25, 15)},
        "eta": {"same_city": "1 天", "same_province": "1-2 天", "cross_province": "2-3 天", "remote": "3-6 天"},
        "eta_days": {"same_city": 1, "same_province": 1.5, "cross_province": 2.5, "remote": 4.5},
        "insurance": "flat",
        "max_kg": 100,
        "tags": ["稳妥", "服务好", "大件也接"],
        "app": "京东 APP「寄快递」/ 京东快递小程序",
        "risk": "价格居中,偏远地区覆盖不如 EMS",
    },
    {
        "code": "sf_standard",
        "name": "顺丰标快",
        "short": "顺丰标快",
        "first_kg": 1,
        "divisor": 6000,
        "price": {"same_city": (12, 2), "same_province": (13, 3), "cross_province": (20, 8), "remote": (25, 15)},
        "eta": {"same_city": "当日/次日", "same_province": "次日", "cross_province": "2-3 天", "remote": "3-5 天"},
        "eta_days": {"same_city": 1, "same_province": 1, "cross_province": 2.5, "remote": 4},
        "insurance": "sf",
        "max_kg": 100,
        "tags": ["稳妥", "贵重", "易碎", "时效有保障"],
        "app": "顺丰速运 APP / 微信小程序「顺丰速运+」",
        "risk": "续重贵,重货不划算(超过 20kg 看顺丰重货或德邦)",
    },
    {
        "code": "sf_express",
        "name": "顺丰特快",
        "short": "顺丰特快",
        "first_kg": 1,
        "divisor": 6000,
        "price": {"same_city": (14, 3), "same_province": (15, 3), "cross_province": (23, 13), "remote": (30, 20)},
        "eta": {"same_city": "当日", "same_province": "次日上午", "cross_province": "1-2 天", "remote": "2-4 天"},
        "eta_days": {"same_city": 0.5, "same_province": 1, "cross_province": 1.5, "remote": 3},
        "insurance": "sf",
        "max_kg": 100,
        "tags": ["最快", "急件", "文件证件"],
        "app": "顺丰速运 APP(下单时选“特快”)",
        "risk": "最贵;非急件没必要",
    },
    {
        "code": "ems",
        "name": "EMS(邮政特快专递)",
        "short": "EMS",
        "first_kg": 1,
        "divisor": 6000,
        "price": {"same_city": (12, 6), "same_province": (15, 8), "cross_province": (22, 12), "remote": (24, 12)},
        "eta": {"same_city": "1 天", "same_province": "1-2 天", "cross_province": "2-5 天", "remote": "4-7 天"},
        "eta_days": {"same_city": 1, "same_province": 1.5, "cross_province": 3.5, "remote": 5.5},
        "insurance": "ems",
        "max_kg": 50,
        "tags": ["偏远地区不加价", "全国覆盖到村", "证件文件首选"],
        "app": "EMS 中国邮政速递物流 APP / 微信小程序「EMS 中国邮政速递物流」",
        "risk": "非偏远地区价格偏高,时效一般",
    },
    {
        "code": "debang",
        "name": "德邦快递(大件 / 重货)",
        "short": "德邦",
        "first_kg": 3,
        "divisor": 6000,
        "price": {"same_city": (15, 1.5), "same_province": (18, 2), "cross_province": (25, 3), "remote": (40, 10)},
        "eta": {"same_city": "1-2 天", "same_province": "2-3 天", "cross_province": "3-6 天", "remote": "6-10 天"},
        "eta_days": {"same_city": 1.5, "same_province": 2.5, "cross_province": 4.5, "remote": 8},
        "insurance": "flat",
        "max_kg": 500,
        "tags": ["大件", "重货最划算", "上门取送"],
        "app": "德邦快递 APP / 微信小程序「德邦快递」",
        "risk": "3kg 以下不划算;时效偏慢",
    },
]

ITEM_KEYWORDS = {
    "fragile": ["易碎", "玻璃", "陶瓷", "瓷", "屏幕", "显示器", "花瓶", "酒", "灯"],
    "valuable": ["贵重", "手机", "笔记本", "电脑", "相机", "镜头", "首饰", "黄金", "手表", "平板", "显卡", "iPhone", "iphone", "Mac", "mac"],
    "document": ["文件", "证件", "合同", "证书", "护照", "档案", "发票", "毕业证", "身份证"],
    "fresh": ["生鲜", "水果", "海鲜", "肉", "蛋糕", "冷冻", "冷藏", "螃蟹", "荔枝"],
    "big": ["家具", "行李", "被子", "书", "电器", "自行车", "轮胎", "桌", "椅", "搬家"],
    "battery": ["电池", "充电宝", "锂电", "电动车", "无人机"],
    "liquid": ["液体", "香水", "护肤", "化妆品", "饮料", "酱", "油", "指甲油"],
}


def classify_item(item_type: str | None) -> set[str]:
    text = item_type or ""
    kinds = set()
    for kind, words in ITEM_KEYWORDS.items():
        if any(w in text for w in words):
            kinds.add(kind)
    return kinds


def insurance_fee(kind: str, declared_value: float | None) -> float:
    if not declared_value or declared_value <= 0:
        return 0.0
    if kind == "sf":
        if declared_value <= 500:
            return 1.0
        if declared_value <= 1000:
            return 2.0
        return math.ceil(declared_value * 0.005)
    if kind == "ems":
        return max(1.0, math.ceil(declared_value * 0.01))
    return max(1.0, math.ceil(declared_value * 0.005))


def estimate(
    from_place: str,
    to_place: str,
    weight_kg: float,
    length_cm: float | None = None,
    width_cm: float | None = None,
    height_cm: float | None = None,
    item_type: str | None = None,
    urgency: str = "normal",
    declared_value: float | None = None,
) -> dict:
    src = resolve_place(from_place)
    dst = resolve_place(to_place)
    zone = zone_between(src, dst)
    weight_kg = max(float(weight_kg or 0.5), 0.1)
    volume_cm3 = None
    if length_cm and width_cm and height_cm:
        volume_cm3 = float(length_cm) * float(width_cm) * float(height_cm)
    kinds = classify_item(item_type)

    quotes = []
    for c in CARRIERS:
        first_price, extra_price = c["price"][zone]
        vol_weight = round(volume_cm3 / c["divisor"], 2) if volume_cm3 else None
        chargeable = max(weight_kg, vol_weight or 0)
        billed = max(math.ceil(chargeable - 1e-9), c["first_kg"])
        base = first_price + max(0, billed - c["first_kg"]) * extra_price
        ins = insurance_fee(c["insurance"], declared_value)
        note = []
        suitable = True
        if chargeable > c["max_kg"]:
            suitable = False
            note.append(f"超过承接上限 {c['max_kg']}kg")
        if c["code"] == "debang" and chargeable < 3:
            suitable = False
            note.append("3kg 以下不划算(首重 3kg)")
        if "fresh" in kinds and c["code"] not in ("sf_standard", "sf_express", "jd"):
            note.append("生鲜不建议,需要冷链(顺丰冷运 / 京东冷链)")
        if ("fragile" in kinds or "valuable" in kinds) and c["code"] in ("platform", "tongda"):
            note.append("贵重 / 易碎物品不建议,理赔体验差")
        if "document" in kinds and c["code"] in ("platform", "tongda", "debang"):
            note.append("证件文件建议顺丰 / EMS,更稳")
        if "big" in kinds and chargeable >= 10 and c["code"] in ("sf_standard", "sf_express", "ems"):
            note.append("重货续重贵,不如德邦 / 京东大件")
        quotes.append(
            {
                "carrier_code": c["code"],
                "carrier": c["name"],
                "short": c["short"],
                "zone": ZONE_LABEL[zone],
                "first_kg": c["first_kg"],
                "first_price": first_price,
                "extra_per_kg": extra_price,
                "volumetric_divisor": c["divisor"],
                "volumetric_weight_kg": vol_weight,
                "chargeable_weight_kg": round(chargeable, 2),
                "billed_weight_kg": billed,
                "shipping_fee": round(base, 1),
                "insurance_fee": ins,
                "total": round(base + ins, 1),
                "eta": c["eta"][zone],
                "eta_days": c["eta_days"][zone],
                "tags": c["tags"],
                "app": c["app"],
                "risk": c["risk"],
                "suitable": suitable,
                "notes": note,
            }
        )

    suitable_quotes = [q for q in quotes if q["suitable"]]
    cheapest = min(suitable_quotes, key=lambda q: (q["total"], q["eta_days"])) if suitable_quotes else None
    fastest = min(suitable_quotes, key=lambda q: (q["eta_days"], q["total"])) if suitable_quotes else None
    safe_pool = [q for q in suitable_quotes if q["carrier_code"] in ("sf_standard", "jd", "ems")] or suitable_quotes
    safest = min(safe_pool, key=lambda q: (q["total"], q["eta_days"])) if safe_pool else None

    # 综合推荐
    if urgency == "fast" and fastest:
        recommended = fastest
        why = "你要求时效优先"
    elif kinds & {"fragile", "valuable", "document", "fresh"} and safest:
        recommended = safest
        why = "物品贵重 / 易碎 / 重要,稳妥优先"
    elif "big" in kinds and any(q["carrier_code"] == "debang" and q["suitable"] for q in quotes):
        recommended = next(q for q in quotes if q["carrier_code"] == "debang")
        why = "大件重货,德邦按公斤计价最划算"
    else:
        recommended = cheapest
        why = "普通物品,省钱优先"

    tips = []
    if vol := (recommended and recommended.get("volumetric_weight_kg")):
        if vol > weight_kg:
            tips.append(f"体积重 {vol}kg 大于实重 {weight_kg}kg,会按体积重计费,包装尽量压小、别用大箱装小件")
    if declared_value and declared_value >= 500:
        tips.append("物品价值 ≥500 元,强烈建议保价;不保价出问题通常只按运费几倍赔")
    if "battery" in kinds:
        tips.append("含锂电池:多数快递要求电池装在设备里、单独锂电池/充电宝很多网点拒收,下单前先问")
    if "liquid" in kinds:
        tips.append("液体:要密封 + 防漏,香水、酒精类很多快递拒收或走陆运")
    if "fresh" in kinds:
        tips.append("生鲜:选冷链(顺丰冷运 / 京东冷链),加冰袋 + 泡沫箱,避开周五晚寄出防止周末滞留")
    if "fragile" in kinds:
        tips.append("易碎:气泡膜裹 3 层 + 箱内填充 + 贴“易碎”标,并让快递员当面验视再封箱")
    tips.append("寄前拍照:物品状态 + 包装 + 面单各拍一张,出问题就是证据")

    return {
        "from": src,
        "to": dst,
        "zone": zone,
        "zone_label": ZONE_LABEL[zone],
        "actual_weight_kg": weight_kg,
        "volume_cm3": volume_cm3,
        "item_kinds": sorted(kinds),
        "urgency": urgency,
        "declared_value": declared_value,
        "quotes": sorted(quotes, key=lambda q: (not q["suitable"], q["total"])),
        "recommended": recommended,
        "recommended_reason": why,
        "cheapest": cheapest,
        "fastest": fastest,
        "safest": safest,
        "tips": tips,
        "disclaimer": "价格为公开渠道参考价,各网点 / 平台优惠差异大,以下单页面实际报价为准;体积重公式 长×宽×高(cm)÷6000 或 ÷8000,取实重与体积重较大者计费。",
    }
