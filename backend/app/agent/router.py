"""意图识别:把用户第一句话路由到三个 Agent 之一(关键词打分,零成本、离线可用)。"""
from __future__ import annotations

SIGNALS: dict[str, list[tuple[str, int]]] = {
    "claim": [
        ("破损", 5), ("损坏", 5), ("摔坏", 5), ("碎了", 5), ("坏了", 4), ("压坏", 5), ("变形", 3),
        ("丢了", 5), ("丢失", 5), ("不见", 4), ("没收到", 4), ("少了", 3), ("短少", 5), ("空包", 5),
        ("延误", 5), ("一直不动", 4), ("好几天没", 4), ("超时", 3), ("迟迟", 4),
        ("索赔", 6), ("赔偿", 6), ("理赔", 6), ("投诉", 4), ("维权", 5), ("保价", 4),
        ("冒领", 5), ("误签", 4), ("被签收", 4), ("驿站", 2), ("12305", 6),
    ],
    "return": [
        ("退货", 6), ("退款", 5), ("退掉", 5), ("退了", 4), ("不想要", 4), ("七天无理由", 6),
        ("运费险", 5), ("寄回", 4), ("换货", 3), ("申请退", 5), ("商家不退", 5), ("拒收", 2),
        ("尺码不对", 4), ("买错", 4), ("色差", 3), ("质量问题", 4),
        ("能退", 5), ("还能退", 6), ("可以退", 4), ("退不了", 5), ("不给退", 5), ("能不能退", 6),
    ],
    "ship": [
        ("寄", 4), ("邮寄", 5), ("发货", 3), ("寄件", 6), ("寄快递", 6), ("哪家便宜", 6),
        ("多少钱", 3), ("运费", 3), ("怎么寄", 6), ("上门取件", 4), ("体积重", 5),
        ("寄到", 4), ("寄给", 4), ("发顺丰", 4), ("包装", 2), ("能不能寄", 5), ("禁寄", 4),
    ],
}

# 这些词出现时,明显不是寄件(避免"我寄的快递坏了"被判成寄件)
CLAIM_OVERRIDE = ("坏", "碎", "破", "丢", "少", "赔", "延误", "投诉", "维权")

# 纯知识提问的句式:问规则本身,而不是"我遇到了这个事"
KNOWLEDGE_PATTERNS = (
    "怎么算", "怎么规定", "是什么意思", "什么意思", "有什么规定", "规定是",
    "多长时间", "多少天", "几天内", "算不算", "属于", "定义",
)
# 具体个案的标志:出现这些说明用户在说自己的事,要建任务
PERSONAL_MARKERS = ("我的", "我买", "我寄", "我要", "帮我", "单号", "我在", "我收到", "给我", "我这")


def detect_agent_type(text: str) -> str:
    if not text:
        return "general"
    # 只问规则、不涉及自己具体的事 → 走总台直接查知识库回答,不建任务
    if any(k in text for k in KNOWLEDGE_PATTERNS) and not any(m in text for m in PERSONAL_MARKERS):
        return "general"
    scores = {k: 0 for k in SIGNALS}
    for agent, words in SIGNALS.items():
        for word, weight in words:
            if word in text:
                scores[agent] += weight
    if scores["ship"] > 0 and any(w in text for w in CLAIM_OVERRIDE):
        scores["ship"] = max(0, scores["ship"] - 6)
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 4 else "general"


def guess_title(text: str, agent_type: str) -> str:
    label = {"claim": "索赔", "return": "退货", "ship": "寄件", "general": "咨询"}[agent_type]
    snippet = (text or "").strip().replace("\n", " ")
    return f"{label}:{snippet[:24]}" if snippet else f"{label}任务"
