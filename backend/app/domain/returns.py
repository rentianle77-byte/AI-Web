"""退货资格判断 + 成本分析(依据《消费者权益保护法》第 25 条、《网络购买商品七日无理由退货暂行办法》)。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .shipping import estimate

EXCLUDED_BY_LAW = {
    "定制": "消费者定作的商品(消法第 25 条第一款第一项)",
    "定做": "消费者定作的商品(消法第 25 条第一款第一项)",
    "刻字": "消费者定作的商品(消法第 25 条第一款第一项)",
    "鲜活": "鲜活易腐的商品(消法第 25 条第一款第二项)",
    "生鲜": "鲜活易腐的商品(消法第 25 条第一款第二项)",
    "水果": "鲜活易腐的商品(消法第 25 条第一款第二项)",
    "鲜花": "鲜活易腐的商品(消法第 25 条第一款第二项)",
    "软件": "在线下载或已拆封的音像制品、计算机软件等数字化商品(第三项)",
    "音像": "在线下载或已拆封的音像制品、计算机软件等数字化商品(第三项)",
    "游戏": "在线下载或已拆封的数字化商品(第三项)",
    "报纸": "交付的报纸、期刊(第四项)",
    "期刊": "交付的报纸、期刊(第四项)",
    "杂志": "交付的报纸、期刊(第四项)",
}

# 一经拆封即不视为完好(暂行办法第九条)
OPENED_NOT_INTACT = ["食品", "零食", "保健", "化妆品", "护肤", "口红", "面膜", "医疗", "计生", "避孕", "药"]
# 需购买时确认不宜退货的品类(暂行办法第七条,平台通常标注“不支持七天无理由”)
CONFIRM_REQUIRED = ["内衣", "内裤", "贴身", "泳衣", "袜", "首饰", "黄金", "珠宝", "充值", "虚拟", "卡券", "活体", "宠物", "植物", "大件家具", "定制家具", "拆封激活的手机", "已激活"]


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%m-%d"):
        try:
            d = datetime.strptime(text, fmt).date()
            if fmt == "%m-%d":
                d = d.replace(year=date.today().year)
            return d
        except ValueError:
            continue
    return None


def check_eligibility(
    category: str,
    received_date=None,
    today: date | None = None,
    opened: bool | None = None,
    used: bool | None = None,
    tags_intact: bool | None = None,
    is_custom: bool = False,
    has_shipping_insurance: bool | None = None,
    price: float | None = None,
    platform: str | None = None,
    reason: str = "no_reason",
    merchant_free_return: bool | None = None,
    return_from: str | None = None,
    return_to: str | None = None,
    weight_kg: float | None = None,
) -> dict:
    today = today or date.today()
    recv = _parse_date(received_date)
    category = category or ""
    reason = reason or "no_reason"
    quality_reason = reason in ("quality", "wrong_item", "not_as_described", "damaged", "fake")

    findings: list[str] = []
    blockers: list[str] = []
    basis: list[str] = []
    checklist: list[str] = []

    # ---- 时间窗口:自签收次日起算 7 日 ----
    deadline = None
    days_left = None
    if recv:
        deadline = recv + timedelta(days=7)
        days_left = (deadline - today).days
        if days_left >= 0:
            findings.append(f"签收日 {recv},七天无理由截止 {deadline}(自签收次日起算),还剩 {days_left} 天")
        else:
            findings.append(f"签收日 {recv},七天无理由已于 {deadline} 到期,超出 {-days_left} 天")
            if not quality_reason:
                blockers.append("已超过七天无理由退货期限")
    else:
        findings.append("未提供签收日期,无法判断是否在七天内(请补充)")

    # ---- 法定不适用品类 ----
    for kw, why in EXCLUDED_BY_LAW.items():
        if kw in category:
            if not quality_reason:
                blockers.append(f"品类不适用无理由退货:{why}")
            break
    if is_custom and not quality_reason:
        blockers.append("定制商品不适用无理由退货(消法第 25 条)")

    # ---- 完好判断 ----
    intact = True
    if any(kw in category for kw in OPENED_NOT_INTACT) and opened:
        intact = False
        if not quality_reason:
            blockers.append("食品 / 化妆品 / 医疗器械 / 计生用品一经拆封即不视为完好(暂行办法第九条)")
    if used:
        intact = False
        if not quality_reason:
            blockers.append("已使用且留有使用痕迹,不符合“商品完好”(暂行办法第八、九条)")
    if tags_intact is False:
        intact = False
        if not quality_reason:
            blockers.append("吊牌 / 标识被摘或剪掉,服装鞋帽类不视为完好(暂行办法第九条)")
    if opened and intact:
        findings.append("仅为查验而拆封、合理调试,不影响商品完好(暂行办法第八条)")

    needs_confirm = [kw for kw in CONFIRM_REQUIRED if kw in category]
    if needs_confirm and not quality_reason:
        findings.append(f"“{needs_confirm[0]}”类通常在购买页标注“不支持七天无理由”,以下单时页面确认为准;若下单时未标注则仍可无理由退")

    # ---- 结论 ----
    if quality_reason:
        verdict = "yes"
        summary = "商品有质量问题 / 发错货 / 与描述不符,不受七天无理由限制,可要求退货退款,运费由商家承担"
        basis += [
            "《消费者权益保护法》第 24 条:不符合质量要求的,消费者可以依法退货,或者要求换货、修理;七日内退货,七日后符合法定解除合同条件的可退货",
            "《民法典》第 617 条:标的物不符合质量要求的,买受人可依法要求违约责任",
            "《消费者权益保护法》第 24 条:退货、更换、修理的合理运输费用由经营者承担",
        ]
        who_pays = "商家"
    elif blockers:
        verdict = "no"
        summary = "按现有信息不符合七天无理由退货条件:" + ";".join(blockers)
        basis.append("《消费者权益保护法》第 25 条、《网络购买商品七日无理由退货暂行办法》")
        who_pays = "—"
    elif recv is None or (needs_confirm and not quality_reason):
        verdict = "conditional"
        summary = "大概率可以退,但还差关键信息确认(签收日期 / 购买页是否标注不支持无理由)"
        basis.append("《消费者权益保护法》第 25 条:网购商品自收到之日起七日内可无理由退货,商品应当完好")
        who_pays = "消费者(有运费险或商家承诺包退除外)"
    else:
        verdict = "yes"
        summary = "符合七天无理由退货条件,可以直接申请"
        basis += [
            "《消费者权益保护法》第 25 条:网购商品自收到之日起七日内可无理由退货,且无需说明理由",
            "《网络购买商品七日无理由退货暂行办法》第 8-9 条:能保持原有品质、功能,商品本身、配件、商标标识齐全即视为完好",
            "《消费者权益保护法》第 25 条第三款:经营者应当自收到退回商品之日起七日内返还货款",
        ]
        who_pays = "消费者(有运费险或商家承诺包退除外)"

    # ---- 成本 ----
    cost: dict = {"who_pays_shipping": who_pays}
    if quality_reason:
        cost["note"] = "质量问题退货运费由商家承担:可以先垫付再让商家退运费,或让商家提供上门取件"
    else:
        if has_shipping_insurance:
            cost["note"] = "有运费险:走平台退货流程、由商家确认收货后,保险公司按距离赔 8-25 元左右,通常够覆盖普通快递"
        elif merchant_free_return:
            cost["note"] = "商家承诺“包退 / 上门取件”,按承诺执行,不用自己出运费"
        else:
            cost["note"] = "无运费险且非质量问题,寄回运费自己出;选最便宜的方式寄回"
    if return_from and return_to:
        est = estimate(return_from, return_to, weight_kg or 1.0, urgency="cheapest")
        if est.get("cheapest"):
            cost["cheapest_return_option"] = {
                "carrier": est["cheapest"]["short"],
                "total": est["cheapest"]["total"],
                "eta": est["cheapest"]["eta"],
                "app": est["cheapest"]["app"],
            }
        cost["zone"] = est["zone_label"]

    # ---- 清单 ----
    checklist = [
        "在订单里点“申请退货/退款”,选好原因(质量问题选“质量问题”,不要选“不想要了”)",
        "拍照:商品、吊牌 / 配件、外包装各一张,留底",
        "等商家同意 / 平台给出退货地址后再寄,不要寄到商家聊天里随便给的地址",
        "寄回时保留快递单号并在平台填写,包装完好、原配件齐全",
        "寄出第 3 天看是否签收,签收后第 5 天看退款,没到就催",
    ]

    return {
        "verdict": verdict,
        "summary": summary,
        "findings": findings,
        "blockers": blockers,
        "legal_basis": basis,
        "deadline": str(deadline) if deadline else None,
        "days_left": days_left,
        "intact": intact,
        "cost": cost,
        "checklist": checklist,
        "platform": platform,
        "price": price,
        "reason": reason,
        "disclaimer": "结论依据法规与常见平台规则,个别平台 / 商家有更宽松承诺(如 15 天无理由)以页面为准。",
    }
