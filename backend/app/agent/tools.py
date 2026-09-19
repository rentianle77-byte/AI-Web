"""Agent 可调用的工具集。

每个工具 = 一个 ToolSpec(给模型看的 schema)+ 一个执行函数(在服务端真实跑)。
执行函数拿到 ctx(数据库会话、当前对话、当前任务),返回可 JSON 序列化的结果。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from .. import clock
from ..domain.claims import assess_claim
from ..domain.returns import check_eligibility
from ..domain.shipping import estimate
from ..models import Task
from ..rag.index import index
from ..tasks import service
from ..tasks.workflows import TASK_TYPES, WORKFLOWS
from ..tracking.provider import query_tracking
from ..llm.base import ToolSpec


@dataclass
class ToolContext:
    session: Session
    conversation_id: str
    task: Task | None = None

    def require_task(self) -> Task:
        if self.task is None:
            raise ValueError("当前对话还没有任务,请先调用 create_task 创建任务")
        return self.task


ToolFn = Callable[[ToolContext, dict[str, Any]], Any]
_REGISTRY: dict[str, tuple[ToolSpec, ToolFn]] = {}


def tool(name: str, description: str, parameters: dict) -> Callable[[ToolFn], ToolFn]:
    def deco(fn: ToolFn) -> ToolFn:
        _REGISTRY[name] = (ToolSpec(name=name, description=description, parameters=parameters), fn)
        return fn

    return deco


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


# ============================= 知识检索 =============================
@tool(
    "search_knowledge",
    "检索快递维权知识库(法规原文要点、各家赔付规则、投诉渠道、话术模板)。"
    "任何涉及法条、赔偿标准、时限、平台规则的说法,都必须先查这个工具再回答,不许凭印象说。",
    _obj(
        {
            "query": {"type": "string", "description": "检索问题,用具体关键词,例如「未保价 破损 赔偿 运费倍数」「七天无理由 拆封 完好」"},
            "top_k": {"type": "integer", "description": "返回条数,默认 5", "default": 5},
        },
        ["query"],
    ),
)
def _search_knowledge(ctx: ToolContext, args: dict) -> Any:
    hits = index.search(args["query"], top_k=min(int(args.get("top_k", 5) or 5), 10))
    if not hits:
        return {"hits": [], "note": "知识库没有命中内容,请换关键词再查;仍查不到就如实告诉用户这一点不确定。"}
    return {
        "query": args["query"],
        "hits": [{"来源": f"{h['doc']} / {h['heading']}", "文件": h["source"], "内容": h["text"]} for h in hits],
        "note": "引用时请注明出处文档名。",
    }


# ============================= 物流查询 =============================
@tool(
    "query_tracking",
    "查询快递物流轨迹。返回当前状态、每条轨迹、距上次更新多久。判断延误/丢失必须以这里的数据为准。",
    _obj(
        {
            "tracking_no": {"type": "string", "description": "快递单号"},
            "company": {"type": "string", "description": "快递公司,如 顺丰/中通/圆通/韵达/申通/极兔/京东/EMS/德邦。不填会按单号规则猜"},
            "scenario_hint": {
                "type": "string",
                "enum": ["delivered", "in_transit", "delayed", "lost", "damaged", "exception"],
                "description": "仅演示环境生效:指定要演示的物流情形。用户描述了什么情况就传什么(说包裹坏了传 damaged,说丢了传 lost,说一直不动传 delayed)",
            },
        },
        ["tracking_no"],
    ),
)
def _query_tracking(ctx: ToolContext, args: dict) -> Any:
    return query_tracking(args["tracking_no"], args.get("company"), args.get("scenario_hint"))


# ============================= 索赔评估 =============================
@tool(
    "assess_claim",
    "索赔评估引擎:判断该找谁赔(快递公司还是商家)、能主张多少钱、要哪些证据、怎么一步步升级投诉。"
    "收集到问题类型和物品价值后就调用,不要自己心算赔偿金额。",
    _obj(
        {
            "problem_type": {"type": "string", "enum": ["damaged", "lost", "delayed", "shortage", "wrong_delivery"], "description": "破损/丢失/延误/内件短少/误投冒领"},
            "company": {"type": "string", "description": "快递公司"},
            "declared_value": {"type": "number", "description": "物品实际价值(元)"},
            "insured": {"type": "boolean", "description": "寄件时是否保价"},
            "insured_amount": {"type": "number", "description": "保价金额(元)"},
            "shipping_fee": {"type": "number", "description": "运费(元),不知道默认按 12 元算"},
            "days_delayed": {"type": "number", "description": "已经延误/停更多少天,判断是否达到彻底延误时限"},
            "is_online_purchase": {"type": "boolean", "description": "是不是网购的(网购件签收前风险在卖家,责任方判断完全不同)"},
            "signed": {"type": "boolean", "description": "是否已经签收"},
            "has_evidence": {"type": "boolean", "description": "是否有开箱视频/照片等证据"},
            "item": {"type": "string", "description": "物品名称"},
            "zone": {"type": "string", "enum": ["same_city", "domestic", "international"], "description": "同城/国内异地/国际,影响延误时限判定"},
            "damage_ratio": {"type": "number", "description": "损坏程度 0-1,完全损毁填 1,轻微损坏填 0.3 这样"},
        },
        ["problem_type"],
    ),
)
def _assess_claim(ctx: ToolContext, args: dict) -> Any:
    return assess_claim(**{k: v for k, v in args.items() if v is not None})


# ============================= 退货资格 =============================
@tool(
    "check_return_eligibility",
    "退货资格与成本分析:能不能退(七天无理由/质量问题)、法律依据、运费谁出、有没有运费险、寄回最省钱的方式、退货五步清单。",
    _obj(
        {
            "category": {"type": "string", "description": "商品品类描述,越具体越好,如「运动鞋」「面膜」「定制刻字手链」「已激活的手机」"},
            "received_date": {"type": "string", "description": "签收日期 YYYY-MM-DD"},
            "opened": {"type": "boolean", "description": "是否已拆封"},
            "used": {"type": "boolean", "description": "是否已使用、有使用痕迹"},
            "tags_intact": {"type": "boolean", "description": "吊牌/标识是否完整"},
            "is_custom": {"type": "boolean", "description": "是否定制款"},
            "has_shipping_insurance": {"type": "boolean", "description": "是否有运费险"},
            "merchant_free_return": {"type": "boolean", "description": "商家是否承诺包退/上门取件"},
            "price": {"type": "number", "description": "商品价格(元)"},
            "platform": {"type": "string", "description": "平台:淘宝/天猫/京东/拼多多/抖音/小红书"},
            "reason": {"type": "string", "enum": ["no_reason", "quality", "wrong_item", "not_as_described", "damaged", "fake"], "description": "退货原因,质量类原因不受七天限制且运费归商家"},
            "return_from": {"type": "string", "description": "寄回的出发城市"},
            "return_to": {"type": "string", "description": "商家收货城市"},
            "weight_kg": {"type": "number", "description": "包裹重量(公斤)"},
        },
        ["category"],
    ),
)
def _check_return(ctx: ToolContext, args: dict) -> Any:
    return check_eligibility(**{k: v for k, v in args.items() if v is not None})


# ============================= 寄件比价 =============================
@tool(
    "compare_shipping",
    "寄件方案对比:按区域+实重+体积重算出各家价格与时效,给出最便宜/最快/最稳三种选择和推荐理由,附下单指引与避坑提醒。",
    _obj(
        {
            "from_place": {"type": "string", "description": "寄出地,省或市,如「杭州」「浙江杭州」"},
            "to_place": {"type": "string", "description": "寄达地"},
            "weight_kg": {"type": "number", "description": "实际重量(公斤)"},
            "length_cm": {"type": "number", "description": "包裹长(厘米),有尺寸就填,会算体积重"},
            "width_cm": {"type": "number", "description": "宽(厘米)"},
            "height_cm": {"type": "number", "description": "高(厘米)"},
            "item_type": {"type": "string", "description": "物品描述,如「笔记本电脑」「玻璃摆件」「冬被」「生鲜水果」,影响推荐和避坑提醒"},
            "urgency": {"type": "string", "enum": ["normal", "fast", "cheapest"], "description": "时效要求"},
            "declared_value": {"type": "number", "description": "物品价值(元),用来算保价费"},
        },
        ["from_place", "to_place", "weight_kg"],
    ),
)
def _compare_shipping(ctx: ToolContext, args: dict) -> Any:
    return estimate(**{k: v for k, v in args.items() if v is not None})


# ============================= 任务管理 =============================
@tool(
    "create_task",
    "创建一个可跟踪的任务。用户第一次说明诉求后就创建,之后所有进展都挂在这个任务上。"
    "claim=索赔维权,return=退货管家,ship=寄件决策。",
    _obj(
        {
            "task_type": {"type": "string", "enum": list(TASK_TYPES), "description": "任务类型"},
            "title": {"type": "string", "description": "一句话任务标题,如「中通寄的玻璃杯破损索赔」"},
            "details": {"type": "object", "description": "已知的结构化信息,如 {\"快递公司\":\"中通\",\"单号\":\"7312...\",\"物品价值\":800}"},
        },
        ["task_type", "title"],
    ),
)
def _create_task(ctx: ToolContext, args: dict) -> Any:
    task = service.create_task(ctx.session, args["task_type"], args["title"], args.get("details") or {}, ctx.conversation_id)
    ctx.task = task
    d = service.task_to_dict(task)
    d["steps_outline"] = [f"{s['key']}: {s['title']} —— {s['hint']}" for s in task.steps]
    return d


@tool(
    "update_task",
    "更新任务进度:推进步骤、记录已办事项、补充收集到的信息、改任务状态。"
    "每完成一个实质动作就更新一次,这样用户在右侧面板能看到进度。",
    _obj(
        {
            "step_key": {"type": "string", "description": "要更新的步骤 key(见 create_task 返回的 steps_outline)"},
            "step_status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped"], "description": "该步骤的新状态"},
            "note": {"type": "string", "description": "这一步的结论或备注,会显示在进度条上,写具体一点"},
            "details": {"type": "object", "description": "要合并进任务的结构化信息"},
            "status": {"type": "string", "enum": ["active", "waiting", "attention", "done", "cancelled"], "description": "任务整体状态:active进行中 waiting等对方回复 attention需要用户处理 done完成"},
        },
    ),
)
def _update_task(ctx: ToolContext, args: dict) -> Any:
    task = ctx.require_task()
    updated = service.update_task(
        ctx.session,
        task.id,
        step_key=args.get("step_key"),
        step_status=args.get("step_status"),
        note=args.get("note"),
        details=args.get("details"),
        status=args.get("status"),
    )
    ctx.task = updated
    return service.task_to_dict(updated)


# ============================= 主动跟进 =============================
@tool(
    "schedule_followup",
    "安排主动跟进:到点后系统会自己在对话里提醒用户,不需要用户来问。"
    "这是本产品的核心能力 —— 凡是「等对方回复」「几天后要确认」的环节都要排跟进。"
    "典型:寄回后第 3 天问签收、第 5 天问退款、报损后第 7 天问快递公司答复。",
    _obj(
        {
            "delay_hours": {"type": "number", "description": "多少小时后提醒。3 天=72,5 天=120,7 天=168"},
            "message": {"type": "string", "description": "到点时要对用户说的话,要具体:核对什么、没进展该做什么"},
            "kind": {"type": "string", "enum": ["check", "remind", "escalate"], "description": "check=问进展 remind=提醒用户做事 escalate=该升级投诉了"},
        },
        ["delay_hours", "message"],
    ),
)
def _schedule_followup(ctx: ToolContext, args: dict) -> Any:
    task = ctx.require_task()
    f = service.schedule_followup_in(ctx.session, task.id, ctx.conversation_id, float(args["delay_hours"]), args["message"], args.get("kind", "check"))
    return service.followup_to_dict(f)


@tool(
    "list_followups",
    "查看当前任务已排的跟进计划。",
    _obj({}),
)
def _list_followups(ctx: ToolContext, args: dict) -> Any:
    task = ctx.require_task()
    return [service.followup_to_dict(f) for f in service.list_followups(ctx.session, task_id=task.id)]


@tool(
    "cancel_followups",
    "取消当前任务下所有未触发的跟进(事情已经解决时调用)。",
    _obj({}),
)
def _cancel_followups(ctx: ToolContext, args: dict) -> Any:
    task = ctx.require_task()
    n = service.cancel_followups(ctx.session, task.id)
    return {"cancelled": n}


# ============================= 材料生成 =============================
@tool(
    "save_material",
    "保存一份用户可直接复制/导出的材料(话术、申请书、投诉信、清单、指引)。"
    "内容必须是成品 —— 用户复制出去就能直接发给客服/商家,不要写「请自行填写」之类的占位说明,"
    "已知的信息(单号、金额、日期、公司)必须填进去。",
    _obj(
        {
            "title": {"type": "string", "description": "材料标题"},
            "kind": {"type": "string", "enum": ["script", "letter", "checklist", "guide", "complaint", "other"], "description": "script话术 letter申请书 checklist清单 guide指引 complaint申诉材料"},
            "content": {"type": "string", "description": "完整的 Markdown 内容"},
        },
        ["title", "kind", "content"],
    ),
)
def _save_material(ctx: ToolContext, args: dict) -> Any:
    task = ctx.task
    m = service.save_material(ctx.session, task.id if task else None, ctx.conversation_id, args["title"], args["kind"], args["content"])
    return service.material_to_dict(m)


@tool("list_materials", "列出当前任务已生成的材料。", _obj({}))
def _list_materials(ctx: ToolContext, args: dict) -> Any:
    task = ctx.task
    mats = service.list_materials(ctx.session, task_id=task.id if task else None, conversation_id=None if task else ctx.conversation_id)
    return [{"id": m.id, "title": m.title, "kind": m.kind} for m in mats]


@tool("get_current_time", "获取当前日期时间。凡是要算天数、定期限、判断是否超时,都先调它,不要假设今天是哪天。", _obj({}))
def _get_time(ctx: ToolContext, args: dict) -> Any:
    now = clock.now_local()
    return {"now": clock.fmt_local(clock.now()), "date": now.strftime("%Y-%m-%d"), "weekday": "一二三四五六日"[now.weekday()], "timezone": "Asia/Shanghai"}


# ============================= 导出 =============================
def get_tools(agent_type: str) -> list[ToolSpec]:
    """按 Agent 类型给不同的工具子集,减少模型选错工具的概率。"""
    common = ["search_knowledge", "get_current_time", "create_task", "update_task", "schedule_followup", "list_followups", "cancel_followups", "save_material", "list_materials"]
    extra = {
        "claim": ["query_tracking", "assess_claim"],
        "return": ["check_return_eligibility", "compare_shipping", "query_tracking"],
        "ship": ["compare_shipping", "query_tracking"],
    }.get(agent_type, ["query_tracking", "assess_claim", "check_return_eligibility", "compare_shipping"])
    names = common + extra
    return [_REGISTRY[n][0] for n in names if n in _REGISTRY]


def execute(name: str, ctx: ToolContext, args: dict) -> tuple[str, bool]:
    """执行工具,返回 (JSON 字符串, 是否出错)。"""
    entry = _REGISTRY.get(name)
    if not entry:
        return json.dumps({"error": f"没有名为 {name} 的工具"}, ensure_ascii=False), True
    _, fn = entry
    try:
        result = fn(ctx, args or {})
        return json.dumps(result, ensure_ascii=False, default=str), False
    except ValueError as exc:
        return json.dumps({"error": str(exc), "hint": "修正参数后重试,或先创建任务"}, ensure_ascii=False), True
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), True


def tool_display_name(name: str) -> str:
    return {
        "search_knowledge": "查知识库",
        "query_tracking": "查物流",
        "assess_claim": "评估索赔",
        "check_return_eligibility": "判断退货资格",
        "compare_shipping": "对比寄件方案",
        "create_task": "创建任务",
        "update_task": "更新进度",
        "schedule_followup": "安排跟进",
        "list_followups": "查看跟进",
        "cancel_followups": "取消跟进",
        "save_material": "生成材料",
        "list_materials": "查看材料",
        "get_current_time": "获取当前时间",
    }.get(name, name)


ALL_TOOL_NAMES = tuple(_REGISTRY.keys())
