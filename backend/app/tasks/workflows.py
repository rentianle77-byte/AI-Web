"""三类任务的状态机定义(步骤即状态,顺序推进,允许跳过)。"""
from __future__ import annotations

WORKFLOWS: dict[str, dict] = {
    "claim": {
        "label": "索赔维权",
        "agent": "索赔维权 Agent",
        "steps": [
            ("collect_info", "收集信息", "快递公司、单号、问题类型(破损/丢失/延误)、物品与价值、是否保价、证据"),
            ("verify_tracking", "核实物流", "查询物流轨迹,确认当前状态、异常节点和时间线"),
            ("assess", "责任判定与赔偿评估", "判断该找快递公司还是商家,依据法规与公司规则估算可主张金额"),
            ("prepare_materials", "准备索赔材料", "生成索赔话术、索赔申请书、证据清单"),
            ("submit", "提交索赔", "向快递公司客服 / 商家正式提出索赔并记录时间"),
            ("await_response", "等待答复并跟进", "7 日内未处理或结果不满意,准备升级"),
            ("escalate", "升级投诉", "12305 邮政业申诉 / 12315 / 平台介入 / 诉讼"),
            ("resolved", "结案", "赔付到账或与对方达成一致"),
        ],
    },
    "return": {
        "label": "退货管家",
        "agent": "退货管家 Agent",
        "steps": [
            ("check_eligibility", "判断退货资格", "是否在七天内、商品是否完好、是否属于不支持无理由退货的品类"),
            ("cost_analysis", "算清成本", "运费谁出、有没有运费险、哪家寄回最便宜"),
            ("apply", "平台申请退货", "在订单里发起退货申请,附上话术"),
            ("ship_back", "寄回商品", "按商家给的地址寄回,保留单号与照片"),
            ("merchant_receive", "商家签收", "寄出后跟进签收情况"),
            ("refund", "退款到账", "签收后跟进退款,未到账则催退"),
            ("resolved", "完成", "退款到账,任务结束"),
        ],
    },
    "ship": {
        "label": "寄件决策",
        "agent": "寄件决策 Agent",
        "steps": [
            ("collect_requirements", "拆解需求", "重量、体积、物品类型、寄达地、时效要求、是否贵重"),
            ("compare_plans", "匹配方案", "对比各家价格与时效,给出最划算 / 最快 / 最稳三种选择"),
            ("guide", "下单指引", "去哪个 APP 下单、怎么填单、要不要保价"),
            ("pitfalls", "避坑提醒", "体积重、包装、违禁品、面单信息"),
            ("order_placed", "已下单", "记录单号,跟踪签收"),
            ("delivered", "已签收", "任务结束"),
        ],
    },
}

TASK_TYPES = tuple(WORKFLOWS.keys())


def new_steps(task_type: str) -> list[dict]:
    wf = WORKFLOWS[task_type]
    steps = []
    for idx, (key, title, hint) in enumerate(wf["steps"]):
        steps.append(
            {
                "key": key,
                "title": title,
                "hint": hint,
                "status": "in_progress" if idx == 0 else "pending",
                "note": "",
                "updated_at": None,
            }
        )
    return steps


def workflow_label(task_type: str) -> str:
    return WORKFLOWS.get(task_type, {}).get("label", task_type)


def steps_outline(task_type: str) -> str:
    wf = WORKFLOWS[task_type]
    return "\n".join(f"{i + 1}. {key}:{title} —— {hint}" for i, (key, title, hint) in enumerate(wf["steps"]))
