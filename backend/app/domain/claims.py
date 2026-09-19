"""索赔评估引擎:责任方判断 + 赔偿估算 + 证据清单 + 升级路径。"""
from __future__ import annotations

import math

from ..tracking.provider import CARRIER_HOTLINES, normalize_company

PROBLEM_LABEL = {"damaged": "破损", "lost": "丢失", "delayed": "延误", "shortage": "内件短少", "wrong_delivery": "误投 / 冒领"}

# 未保价快件各公司格式条款常见赔偿倍数(运费倍数,参考,并非法律上限)
UNINSURED_MULTIPLIER = {
    "顺丰": (3, 7), "京东": (3, 7), "德邦": (3, 5), "EMS": (2, 3), "邮政": (2, 3),
    "中通": (3, 5), "圆通": (3, 5), "韵达": (3, 5), "申通": (3, 5), "极兔": (3, 5),
}

# 《快递服务》国家标准 GB/T 27917.3:彻底延误时限(日历天),超过可视为丢失
TOTAL_DELAY_DAYS = {"same_city": 7, "domestic": 10, "international": 30}
DELAY_DAYS = {"same_city": 3, "domestic": 7, "international": 20}


def assess_claim(
    problem_type: str,
    company: str | None = None,
    declared_value: float | None = None,
    insured: bool = False,
    insured_amount: float | None = None,
    shipping_fee: float | None = None,
    days_delayed: float | None = None,
    is_online_purchase: bool | None = None,
    signed: bool | None = None,
    has_evidence: bool | None = None,
    item: str | None = None,
    zone: str = "domestic",
    damage_ratio: float | None = None,
) -> dict:
    company = normalize_company(company)
    problem_type = problem_type if problem_type in PROBLEM_LABEL else "damaged"
    value = float(declared_value or 0)
    fee = float(shipping_fee or 12)
    zone = zone if zone in TOTAL_DELAY_DAYS else "domestic"

    # ---------- 责任方 ----------
    if is_online_purchase:
        if not signed:
            primary, reason = "商家", "网购商品在签收前毁损、灭失的风险由卖家承担(民法典第 604、512 条),先找商家补发 / 退款,商家再自行向快递公司追偿"
        else:
            primary, reason = "商家(同时向快递公司投诉)", "签收后发现破损,尽快(建议 48 小时内)在平台申请“收到商品破损”并附开箱照片 / 视频;商家应先行处理,快递责任由商家与快递公司之间解决"
        also = "如果商家推诿,可同时向快递公司客服报损、向平台申请介入"
    else:
        primary, reason = "快递公司", "自寄件的运输合同相对方是快递公司,寄件人(或经寄件人授权的收件人)向快递公司索赔"
        also = "先打客服电话报损立案,拿到工单号;网点推诿就直接找总部客服"

    # ---------- 延误判定 ----------
    delay_note = None
    treat_as_lost = False
    if problem_type == "delayed" and days_delayed is not None:
        if days_delayed >= TOTAL_DELAY_DAYS[zone]:
            treat_as_lost = True
            delay_note = f"已超过彻底延误时限({TOTAL_DELAY_DAYS[zone]} 个日历天,《快递服务》国家标准),可按丢失索赔"
        elif days_delayed >= DELAY_DAYS[zone]:
            delay_note = f"已超过正常时限({DELAY_DAYS[zone]} 天),构成延误,可要求免除本次运费并催促投递"
        else:
            delay_note = f"尚未达到延误标准({DELAY_DAYS[zone]} 天),建议先催件,超过 {TOTAL_DELAY_DAYS[zone]} 天可按丢失处理"

    # ---------- 赔偿估算 ----------
    basis: list[str] = []
    scenarios: list[dict] = []
    suggested = None
    bottom = None
    typical = None
    effective_type = "lost" if treat_as_lost else problem_type

    if effective_type == "delayed":
        basis.append("《快递服务》国家标准:快件延误的赔偿为免除本次服务费用(不含保价等附加费用)")
        basis.append("《民法典》第 832 条:承运人对运输过程中货物的毁损、灭失承担赔偿责任;延误造成实际损失的可一并主张(需举证)")
        scenarios.append({"name": "标准处理", "amount": fee, "desc": f"退还本次运费约 {fee:.0f} 元,并要求限期投递"})
        suggested = fee
        bottom = fee
        typical = "退运费"
    else:
        ratio = 1.0
        if effective_type in ("damaged", "shortage"):
            ratio = min(max(damage_ratio or (1.0 if effective_type == "damaged" else 0.5), 0.05), 1.0)
        actual_loss = round(value * ratio, 2) if value else None
        if insured:
            cap = float(insured_amount or value or 0)
            amount = round(min(cap, actual_loss) if actual_loss else cap * ratio, 2)
            basis.append("《快递暂行条例》第 27 条:保价快件按约定的保价规则赔偿")
            basis.append("保价规则通用做法:全损按保价额(不超过实际损失)赔;部分损坏按保价额与物品价值的比例赔")
            scenarios.append({"name": "保价赔付", "amount": amount, "desc": f"保价额 {cap:.0f} 元,损失比例 {ratio:.0%},可主张约 {amount:.0f} 元"})
            suggested = amount
            bottom = amount
            typical = f"按保价赔约 {amount:.0f} 元"
        else:
            lo, hi = UNINSURED_MULTIPLIER.get(company or "", (3, 5))
            offer_lo, offer_hi = round(fee * lo), round(fee * hi)
            basis.append("《快递暂行条例》第 27 条:未保价快件依照民事法律规定确定赔偿责任")
            basis.append("《民法典》第 833 条:货物毁损、灭失的赔偿额按交付时到达地市场价格计算(即实际损失)")
            basis.append("《民法典》第 496、497 条:快递公司“未保价只赔运费 N 倍”的格式条款,未尽提示说明义务或不合理免责的,可主张不成为合同内容 / 无效")
            typical = f"快递公司首次报价通常是运费 {lo}-{hi} 倍 ≈ {offer_lo}-{offer_hi} 元"
            scenarios.append({"name": "公司格式条款方案", "amount": offer_hi, "desc": f"运费 {lo}-{hi} 倍 ≈ {offer_lo}-{offer_hi} 元(客服第一轮基本是这个)"})
            if actual_loss:
                scenarios.append({"name": "主张实际损失", "amount": actual_loss, "desc": f"凭订单 / 发票证明价值 {value:.0f} 元 × 损失比例 {ratio:.0%} = {actual_loss:.0f} 元"})
                negotiated = round(max(offer_hi, actual_loss * 0.6))
                scenarios.append({"name": "协商折中(常见结果)", "amount": negotiated, "desc": f"证据充分时多数在 {negotiated}-{actual_loss:.0f} 元之间达成"})
                suggested = actual_loss
                bottom = max(offer_hi, negotiated)
            else:
                suggested = offer_hi
                bottom = offer_hi
                basis.append("没有价值凭证时很难突破运费倍数,尽量找订单截图 / 发票 / 支付记录")

    # ---------- 时限 ----------
    deadlines = [
        "报损:签收当天最好,最迟 48 小时内向快递 / 商家反馈并留证(越晚越容易被以“签收后损坏”拒赔)",
        "企业处理:向快递企业投诉后,企业应在 7 日内处理并答复;超期或不满意即可升级申诉",
        "12305 申诉:需先向企业投诉,7 日未处理或不满意再申诉;申诉时提供投诉记录 / 工单号",
        "诉讼时效:3 年,但证据会随时间流失,建议 30 天内解决",
    ]

    # ---------- 证据 ----------
    evidence = [
        "快递单号 + 面单照片(寄件人、收件人、公司都要清晰)",
        "外包装照片:6 个面 + 破损处特写,不要先扔箱子",
        "内件损坏照片 / 视频,最好有“未拆封 → 拆封 → 损坏”连续开箱视频",
        "物品价值凭证:订单截图、发票、支付记录、商品链接",
        "与快递员 / 客服 / 商家的沟通记录截图(通话录音注明时间)",
        "保价凭证(如有):运单上的保价金额、保价费",
        "如果是驿站 / 代收点签收:代收点监控或签收记录、是否事先同意放驿站的聊天记录",
    ]
    if problem_type == "lost":
        evidence.insert(2, "物流轨迹截图,标出最后一条更新时间(证明超时)")
    if problem_type == "delayed":
        evidence.insert(2, "物流轨迹截图 + 承诺时效截图(下单页面 / 运单上的时效说明)")

    # ---------- 升级路径 ----------
    hotline = CARRIER_HOTLINES.get(company or "", "各公司官网客服")
    escalation = [
        {"level": 1, "who": f"快递公司客服 {hotline}", "action": "电话报损立案,要求 7 日内答复,记下工单号;网点不认就找总部"},
        {"level": 2, "who": "电商平台(淘宝 / 京东 / 拼多多 / 抖音)", "action": "网购件在订单里申请“退款 / 破损补发”,商家不处理申请平台介入"},
        {"level": 3, "who": "国家邮政局 12305 / 邮政业消费者申诉网站 sswz.spb.gov.cn", "action": "企业 7 日未处理或不满意时申诉,这是最管用的一步,企业会被邮政管理部门督办"},
        {"level": 4, "who": "12315 / 全国 12315 平台", "action": "针对商家(非快递公司)的消费纠纷"},
        {"level": 5, "who": "人民法院(小额诉讼)", "action": "金额较大且证据充分时,可起诉快递公司,诉讼费低"},
    ]

    risks = []
    if signed and problem_type == "damaged" and not has_evidence:
        risks.append("已签收且缺少开箱证据:快递公司很可能以“签收视为验收”拒赔,尽快补拍现状照片、找驿站监控")
    if not insured and value >= 500:
        risks.append("未保价高价值物品:公司条款只赔运费倍数,要靠价值凭证和法条据理力争,做好协商准备")
    if problem_type == "lost" and (days_delayed or 0) < TOTAL_DELAY_DAYS[zone]:
        risks.append(f"尚未超过彻底延误时限({TOTAL_DELAY_DAYS[zone]} 天),快递公司会说“还在途”,先让其核查并出具书面说明")
    if not risks:
        risks.append("证据链完整,按流程推进即可")

    next_actions = [
        f"给 {primary} 正式报损(用生成的话术),拿到工单号",
        "把证据按清单整理成一个文件夹,原图保留",
        "记录每次沟通时间和对方承诺,7 天没结果就升级 12305",
    ]

    return {
        "problem_type": problem_type,
        "problem_label": PROBLEM_LABEL[problem_type],
        "company": company,
        "hotline": hotline,
        "responsible_party": {"primary": primary, "reason": reason, "also": also},
        "delay_assessment": delay_note,
        "treat_as_lost": treat_as_lost,
        "compensation": {
            "insured": insured,
            "declared_value": value or None,
            "shipping_fee": fee,
            "legal_basis": basis,
            "scenarios": scenarios,
            "suggested_claim": suggested,
            "bottom_line": bottom,
            "company_typical_offer": typical,
        },
        "deadlines": deadlines,
        "evidence_checklist": evidence,
        "escalation_path": escalation,
        "risks": risks,
        "next_actions": next_actions,
        "disclaimer": "各公司赔偿倍数为公开条款参考值,以其最新官方公示为准;法条引用请用 search_knowledge 核对原文。",
    }
