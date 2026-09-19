"""业务引擎单测:这些是产品的"实际难度五星"部分,必须锁住行为。"""
from datetime import date

import pytest

from app.domain.claims import assess_claim
from app.domain.geo import resolve_place, zone_between
from app.domain.returns import check_eligibility
from app.domain.shipping import estimate


# ---------------- 地域 ----------------
def test_zone_same_city_and_province():
    assert zone_between(resolve_place("杭州"), resolve_place("杭州西湖区")) == "same_city"
    assert zone_between(resolve_place("杭州"), resolve_place("宁波")) == "same_province"
    assert zone_between(resolve_place("北京海淀"), resolve_place("北京朝阳")) == "same_city"
    assert zone_between(resolve_place("杭州"), resolve_place("成都")) == "cross_province"
    assert zone_between(resolve_place("上海"), resolve_place("喀什")) == "remote"


# ---------------- 寄件 ----------------
def test_volumetric_weight_beats_actual():
    """体积重是寄件最大的坑:40×30×30 的箱子实重 2kg,必须按体积重计费。"""
    r = estimate("杭州", "成都", weight_kg=2, length_cm=40, width_cm=30, height_cm=30)
    rec = r["recommended"]
    assert rec["volumetric_weight_kg"] > 2
    assert rec["chargeable_weight_kg"] == rec["volumetric_weight_kg"]
    assert any("体积重" in t for t in r["tips"])


def test_fragile_goes_to_reliable_carrier():
    r = estimate("杭州", "成都", 2, item_type="陶瓷花瓶", declared_value=600)
    assert r["recommended"]["carrier_code"] in ("jd", "sf_standard", "ems")
    assert "fragile" in r["item_kinds"]


def test_urgent_picks_fastest():
    r = estimate("杭州", "成都", 1, urgency="fast")
    assert r["recommended"]["eta_days"] <= r["cheapest"]["eta_days"]


def test_heavy_goods_prefer_debang():
    r = estimate("上海", "武汉", 25, item_type="家具配件")
    assert r["recommended"]["carrier_code"] == "debang"


def test_remote_zone_costs_more():
    near = estimate("上海", "南京", 3)["cheapest"]["total"]
    far = estimate("上海", "喀什", 3)["cheapest"]["total"]
    assert far > near


def test_debang_rejected_for_light_parcels():
    r = estimate("杭州", "北京", 1)
    debang = next(q for q in r["quotes"] if q["carrier_code"] == "debang")
    assert debang["suitable"] is False


# ---------------- 退货 ----------------
TODAY = date(2026, 9, 19)


def test_within_seven_days_ok():
    r = check_eligibility("运动鞋", received_date="2026-09-16", today=TODAY, tags_intact=True)
    assert r["verdict"] == "yes"
    assert r["days_left"] == 4
    assert any("第 25 条" in b or "25 条" in b for b in r["legal_basis"])


def test_over_seven_days_blocked():
    r = check_eligibility("运动鞋", received_date="2026-09-01", today=TODAY)
    assert r["verdict"] == "no"
    assert any("七天无理由" in b for b in r["blockers"])


def test_quality_issue_ignores_time_limit():
    """质量问题不受七天限制,且运费归商家 —— 这是最容易被商家糊弄的点。"""
    r = check_eligibility("加湿器", received_date="2026-08-01", today=TODAY, reason="quality")
    assert r["verdict"] == "yes"
    assert r["cost"]["who_pays_shipping"] == "商家"


def test_opened_cosmetics_not_intact():
    r = check_eligibility("面膜", received_date="2026-09-18", today=TODAY, opened=True)
    assert r["verdict"] == "no"
    assert r["intact"] is False


def test_custom_goods_excluded():
    r = check_eligibility("定制刻字手链", received_date="2026-09-18", today=TODAY, is_custom=True)
    assert r["verdict"] == "no"


def test_cut_tags_blocks_return():
    r = check_eligibility("连衣裙", received_date="2026-09-18", today=TODAY, tags_intact=False)
    assert r["verdict"] == "no"


def test_missing_date_is_conditional():
    r = check_eligibility("运动鞋")
    assert r["verdict"] == "conditional"


def test_shipping_insurance_noted():
    r = check_eligibility("运动鞋", received_date="2026-09-18", today=TODAY, has_shipping_insurance=True, tags_intact=True)
    assert "运费险" in r["cost"]["note"]


def test_return_cost_includes_cheapest_option():
    r = check_eligibility("运动鞋", received_date="2026-09-18", today=TODAY, tags_intact=True, return_from="杭州", return_to="泉州", weight_kg=1)
    assert "cheapest_return_option" in r["cost"]


# ---------------- 索赔 ----------------
def test_online_purchase_unsigned_blames_merchant():
    """网购件签收前风险在卖家 —— 找错对象会白折腾一周。"""
    r = assess_claim("damaged", company="中通", declared_value=780, is_online_purchase=True, signed=False)
    assert "商家" in r["responsible_party"]["primary"]


def test_self_shipped_blames_carrier():
    r = assess_claim("damaged", company="顺丰", declared_value=3000, is_online_purchase=False)
    assert r["responsible_party"]["primary"] == "快递公司"


def test_insured_claim_capped_by_insured_amount():
    r = assess_claim("lost", company="顺丰", declared_value=3000, insured=True, insured_amount=3000)
    assert r["compensation"]["suggested_claim"] == 3000


def test_uninsured_suggests_actual_loss_above_multiplier():
    r = assess_claim("damaged", company="中通", declared_value=780, shipping_fee=12, insured=False)
    comp = r["compensation"]
    assert comp["suggested_claim"] == 780
    assert comp["bottom_line"] < comp["suggested_claim"]
    assert len(comp["scenarios"]) >= 3


def test_delay_beyond_total_limit_treated_as_lost():
    """国内异地超过 10 个日历天 = 彻底延误,可按丢失索赔。"""
    r = assess_claim("delayed", company="中通", declared_value=500, days_delayed=12, zone="domestic")
    assert r["treat_as_lost"] is True
    assert r["compensation"]["suggested_claim"] == 500


def test_delay_within_limit_only_refunds_fee():
    r = assess_claim("delayed", company="中通", shipping_fee=15, days_delayed=8, zone="domestic")
    assert r["treat_as_lost"] is False
    assert r["compensation"]["suggested_claim"] == 15


def test_escalation_includes_12305():
    r = assess_claim("lost", company="韵达", declared_value=400)
    assert any("12305" in step["who"] for step in r["escalation_path"])


def test_partial_damage_scales_claim():
    full = assess_claim("damaged", declared_value=1000, damage_ratio=1.0)["compensation"]["suggested_claim"]
    half = assess_claim("damaged", declared_value=1000, damage_ratio=0.5)["compensation"]["suggested_claim"]
    assert half < full
