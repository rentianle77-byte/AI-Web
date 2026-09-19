"""演示用假模型:没配任何 API key 时也能把三条 Agent 流程完整跑通。

它不"思考",而是按规则推进:看当前任务缺什么信息就问什么,信息齐了就调对应工具。
目的是保证 —— 断网 / 没 key 的场合(比赛现场)依然能演示状态机、工具链、跟进、导出。
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

from .base import LLMClient, LLMEvent, ToolCall, ToolSpec

_TRACKING_RE = re.compile(r"\b([A-Z]{2}\d{8,}|\d{10,15})\b")
_WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:公斤|kg|KG|千克)")
_MONEY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|RMB)")

_CITY_RE = re.compile(r"(?:从|由)\s*([一-龥]{2,6}?)\s*(?:寄|发|到|送)")
_DEST_RE = re.compile(r"(?:到|寄到|发到|送到|寄往)\s*([一-龥]{2,6})")


def _counter(state: dict, key: str) -> int:
    state[key] = state.get(key, 0) + 1
    return state[key]


class MockClient(LLMClient):
    provider = "mock"
    model = "mock-demo"

    def __init__(self) -> None:
        self._state: dict[str, int] = {}

    async def astream(self, system: str, messages: list[dict], tools: list[ToolSpec] | None = None) -> AsyncIterator[LLMEvent]:
        tool_names = {t.name for t in (tools or [])}
        last_user = ""
        for m in reversed(messages):
            if m["role"] == "user" and isinstance(m.get("content"), str):
                last_user = m["content"]
                break
        tool_results = [m for m in messages if m["role"] == "tool"]
        used_tools = {m.get("name") for m in tool_results}
        text = last_user

        step = _counter(self._state, "step")
        prefix = "【演示模型】未配置大模型 API Key,当前由规则引擎驱动流程(真实效果请在 backend/.env 里配置 key)。\n\n"

        # ---- 第一步:识别意图并建任务 ----
        if "create_task" in tool_names and "create_task" not in used_tools:
            if any(k in text for k in ("坏", "破损", "碎", "丢", "少", "延误", "没到", "赔")):
                ttype, title = "claim", "快递索赔"
            elif any(k in text for k in ("退货", "退款", "退掉", "不想要", "退了")):
                ttype, title = "return", "退货处理"
            elif any(k in text for k in ("寄", "发货", "邮寄", "快递过去")):
                ttype, title = "ship", "寄件方案"
            else:
                yield LLMEvent(type="text_delta", text=prefix + "我可以帮你处理三类事:快递破损丢失索赔、退货全流程、寄件选哪家。你说一句具体情况就行。")
                yield LLMEvent(type="done", stop_reason="end_turn")
                return
            yield LLMEvent(type="text_delta", text=prefix + f"我先给你建一个任务来跟踪进度。\n")
            yield LLMEvent(
                type="tool_call",
                tool_call=ToolCall(id=f"mock_{step}", name="create_task", arguments={"task_type": ttype, "title": title, "details": {"用户原话": text[:100]}}),
            )
            yield LLMEvent(type="done", stop_reason="tool_use")
            return

        # ---- 索赔:查物流 ----
        if "query_tracking" in tool_names and "query_tracking" not in used_tools:
            m = _TRACKING_RE.search(text)
            if m:
                yield LLMEvent(type="text_delta", text="拿到单号了,我先查一下物流轨迹。\n")
                yield LLMEvent(type="tool_call", tool_call=ToolCall(id=f"mock_{step}", name="query_tracking", arguments={"tracking_no": m.group(1)}))
                yield LLMEvent(type="done", stop_reason="tool_use")
                return

        # ---- 索赔:评估 ----
        if "assess_claim" in tool_names and "assess_claim" not in used_tools and any(k in text for k in ("坏", "破损", "碎", "丢", "延误", "赔")):
            problem = "damaged" if any(k in text for k in ("坏", "破", "碎")) else ("lost" if "丢" in text else "delayed")
            value = float(_MONEY_RE.search(text).group(1)) if _MONEY_RE.search(text) else None
            yield LLMEvent(type="text_delta", text="我来判断责任方和能主张多少赔偿。\n")
            insured = "保价" in text and not any(n + "保价" in text for n in ("没", "未", "无", "不"))
            yield LLMEvent(
                type="tool_call",
                tool_call=ToolCall(
                    id=f"mock_{step}",
                    name="assess_claim",
                    arguments={"problem_type": problem, "declared_value": value, "insured": insured, "is_online_purchase": any(k in text for k in ("买", "淘宝", "京东", "拼多多", "商家"))},
                ),
            )
            yield LLMEvent(type="done", stop_reason="tool_use")
            return

        # ---- 退货:资格判断 ----
        if "check_return_eligibility" in tool_names and "check_return_eligibility" not in used_tools:
            yield LLMEvent(type="text_delta", text="我先判断这单能不能退。\n")
            yield LLMEvent(
                type="tool_call",
                tool_call=ToolCall(id=f"mock_{step}", name="check_return_eligibility", arguments={"category": text[:20], "reason": "quality" if any(k in text for k in ("质量", "坏", "不对", "假")) else "no_reason"}),
            )
            yield LLMEvent(type="done", stop_reason="tool_use")
            return

        # ---- 寄件:比价 ----
        if "compare_shipping" in tool_names and "compare_shipping" not in used_tools:
            weight = float(_WEIGHT_RE.search(text).group(1)) if _WEIGHT_RE.search(text) else 1.0
            src = _CITY_RE.search(text)
            dst = _DEST_RE.search(text)
            yield LLMEvent(type="text_delta", text="我来对比各家价格和时效。\n")
            yield LLMEvent(
                type="tool_call",
                tool_call=ToolCall(
                    id=f"mock_{step}",
                    name="compare_shipping",
                    arguments={"from_place": src.group(1) if src else "杭州", "to_place": dst.group(1) if dst else "北京", "weight_kg": weight},
                ),
            )
            yield LLMEvent(type="done", stop_reason="tool_use")
            return

        # ---- 推进任务进度(让右侧进度条动起来)----
        if "update_task" in tool_names and "update_task" not in used_tools and tool_results:
            # 步骤 key 是按任务类型定义的,必须先知道当前是哪类任务,否则会传错 key
            task_type = None
            for m in tool_results:
                if m.get("name") == "create_task":
                    try:
                        task_type = json.loads(m["content"]).get("type")
                    except Exception:
                        pass
            done_step = {
                "claim": {
                    "query_tracking": ("verify_tracking", "已核实物流轨迹"),
                    "assess_claim": ("assess", "已判定责任方与赔偿区间"),
                },
                "return": {
                    "check_return_eligibility": ("check_eligibility", "已判定退货资格"),
                    "compare_shipping": ("cost_analysis", "已算清寄回成本"),
                },
                "ship": {
                    "compare_shipping": ("compare_plans", "已对比各家方案"),
                    "query_tracking": ("order_placed", "已记录单号"),
                },
            }.get(task_type or "", {})
            for m in reversed(tool_results):
                if m.get("name") in done_step:
                    key, note = done_step[m["name"]]
                    yield LLMEvent(type="text_delta", text="我把进度更新一下。\n")
                    yield LLMEvent(type="tool_call", tool_call=ToolCall(id=f"mock_{step}", name="update_task", arguments={"step_key": key, "step_status": "done", "note": note}))
                    yield LLMEvent(type="done", stop_reason="tool_use")
                    return

        # ---- 生成材料 ----
        if "save_material" in tool_names and "save_material" not in used_tools and tool_results:
            yield LLMEvent(type="text_delta", text="我把可以直接用的话术整理出来。\n")
            yield LLMEvent(
                type="tool_call",
                tool_call=ToolCall(
                    id=f"mock_{step}",
                    name="save_material",
                    arguments={
                        "title": "客服交涉话术(演示)",
                        "kind": "script",
                        "content": "您好,我的快递出现问题,单号 XXXX。\n事实:XXXX。\n依据:《快递暂行条例》第 27 条、《民法典》第 833 条。\n诉求:按实际损失赔偿,请在 7 日内给我书面答复并提供工单号。",
                    },
                ),
            )
            yield LLMEvent(type="done", stop_reason="tool_use")
            return

        # ---- 排跟进 ----
        if "schedule_followup" in tool_names and "schedule_followup" not in used_tools and tool_results:
            yield LLMEvent(type="text_delta", text="我给你排一个跟进提醒。\n")
            yield LLMEvent(
                type="tool_call",
                tool_call=ToolCall(id=f"mock_{step}", name="schedule_followup", arguments={"delay_hours": 72, "message": "该问问对方处理进展了,没动静就升级投诉。", "kind": "check"}),
            )
            yield LLMEvent(type="done", stop_reason="tool_use")
            return

        # ---- 收尾:把工具结果讲清楚 ----
        summary_lines = ["我已经按流程处理完这一轮:\n"]
        for m in tool_results[-6:]:
            try:
                data = json.loads(m["content"])
            except Exception:
                data = {}
            name = m.get("name")
            if name == "query_tracking":
                summary_lines.append(f"- 物流:{data.get('company')} {data.get('tracking_no')},当前状态「{data.get('status')}」")
            elif name == "assess_claim":
                comp = data.get("compensation", {})
                summary_lines.append(f"- 责任方:{data.get('responsible_party', {}).get('primary')};建议主张 {comp.get('suggested_claim')} 元,底线 {comp.get('bottom_line')} 元")
            elif name == "check_return_eligibility":
                summary_lines.append(f"- 退货资格:{data.get('verdict')} —— {data.get('summary')}")
            elif name == "compare_shipping":
                rec = data.get("recommended") or {}
                summary_lines.append(f"- 推荐 {rec.get('short')},约 {rec.get('total')} 元,{rec.get('eta')}")
            elif name == "create_task":
                summary_lines.append(f"- 已建任务 {data.get('id')}:{data.get('title')}")
            elif name == "save_material":
                summary_lines.append(f"- 已生成材料:{data.get('title')}(右侧「材料」里可复制导出)")
            elif name == "schedule_followup":
                summary_lines.append(f"- 已排跟进:{data.get('due_at_text')} 会主动提醒你")
        summary_lines.append("\n右侧面板能看到任务进度、跟进计划和生成的材料。配置真实模型 Key 后,这里会变成有理有据的完整分析。")
        yield LLMEvent(type="text_delta", text=prefix + "\n".join(summary_lines))
        yield LLMEvent(type="done", stop_reason="end_turn")
