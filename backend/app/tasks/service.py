"""任务 / 跟进 / 材料的读写与序列化。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..events import bus
from ..models import Conversation, Followup, Material, Task
from .workflows import TASK_TYPES, new_steps, workflow_label

STEP_STATUSES = ("pending", "in_progress", "done", "skipped")
TASK_STATUSES = ("active", "waiting", "attention", "done", "cancelled")


# ---------------- 序列化 ----------------
def task_to_dict(task: Task) -> dict:
    steps = task.steps or []
    total = len(steps)
    done = sum(1 for s in steps if s.get("status") in ("done", "skipped"))
    current = next((s for s in steps if s.get("key") == task.current_step), None)
    return {
        "id": task.id,
        "type": task.type,
        "type_label": workflow_label(task.type),
        "title": task.title,
        "status": task.status,
        "current_step": task.current_step,
        "current_step_title": current.get("title") if current else None,
        "progress": round(done / total * 100) if total else 0,
        "details": task.details or {},
        "steps": steps,
        "conversation_id": task.conversation_id,
        "created_at": clock.iso(task.created_at),
        "updated_at": clock.iso(task.updated_at),
    }


def followup_to_dict(f: Followup) -> dict:
    return {
        "id": f.id,
        "task_id": f.task_id,
        "conversation_id": f.conversation_id,
        "due_at": clock.iso(f.due_at),
        "due_at_text": clock.fmt_local(f.due_at),
        "message": f.message,
        "kind": f.kind,
        "status": f.status,
        "fired_at": clock.iso(f.fired_at),
        "created_at": clock.iso(f.created_at),
    }


def material_to_dict(m: Material) -> dict:
    return {
        "id": m.id,
        "task_id": m.task_id,
        "conversation_id": m.conversation_id,
        "title": m.title,
        "kind": m.kind,
        "content": m.content,
        "created_at": clock.iso(m.created_at),
    }


# 三类任务的步骤名各不相同,模型偶尔会把另一套的名字用过来。
# 这里给常见的错配一个归宿,免得一个纯粹的命名问题打断整条流程。
STEP_ALIASES: dict[str, tuple[str, ...]] = {
    "collect_info": ("collect", "collect_requirements", "gather", "info", "收集信息"),
    "verify_tracking": ("tracking", "check_tracking", "query_tracking", "logistics", "核实物流"),
    "assess": ("assessment", "evaluate", "judge", "responsibility", "评估", "责任判定"),
    "prepare_materials": ("materials", "prepare", "draft", "准备材料"),
    "submit": ("submit_claim", "file_claim", "apply_claim", "提交"),
    "await_response": ("waiting", "await", "follow_up", "等待答复"),
    "escalate": ("escalation", "complain", "升级"),
    "resolved": ("done", "finish", "complete", "close", "结案", "完成"),
    "check_eligibility": ("eligibility", "check", "判断资格", "资格"),
    "cost_analysis": ("cost", "compare_shipping", "shipping_cost", "算成本", "成本"),
    "apply": ("apply_return", "request", "申请"),
    "ship_back": ("return_ship", "send_back", "寄回"),
    "merchant_receive": ("receive", "merchant", "签收"),
    "refund": ("refund_received", "money_back", "退款"),
    "collect_requirements": ("requirements", "collect_info", "需求"),
    "compare_plans": ("compare", "compare_shipping", "plans", "比价", "对比方案"),
    "guide": ("instructions", "howto", "指引"),
    "pitfalls": ("tips", "warnings", "避坑"),
    "order_placed": ("ordered", "placed", "已下单"),
    "delivered": ("signed", "received", "已签收"),
}


def _normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum() or "一" <= ch <= "鿿")


def resolve_step(steps: list[dict], wanted: str, current_step: str | None) -> tuple[int | None, str | None]:
    """把模型给的步骤名解析成实际下标。

    模型面对的是三套不同的步骤命名,难免串台(比如给退货任务传了寄件的
    compare_plans)。精确匹配失败就直接抛错的话,一个纯命名问题会在工具轨迹上
    留下一串红叉。这里按 精确 → 标题 → 别名 → 模糊 的顺序找,
    实在找不到才落到当前步骤,并把这件事写进返回值告诉模型。

    返回 (下标, 说明)。说明非空时表示做了兜底,需要回给模型。
    """
    if not steps:
        return None, None
    norm_wanted = _normalize(wanted)

    for i, s in enumerate(steps):
        if s["key"] == wanted:
            return i, None
    for i, s in enumerate(steps):
        if _normalize(s["key"]) == norm_wanted or _normalize(s.get("title", "")) == norm_wanted:
            return i, f"步骤名 {wanted} 已按标题匹配到 {s['key']}"
    for i, s in enumerate(steps):
        aliases = STEP_ALIASES.get(s["key"], ())
        if wanted in aliases or norm_wanted in {_normalize(a) for a in aliases}:
            return i, f"步骤名 {wanted} 已按别名匹配到 {s['key']}"
    for i, s in enumerate(steps):
        nk, nt = _normalize(s["key"]), _normalize(s.get("title", ""))
        if norm_wanted and (norm_wanted in nk or nk in norm_wanted or norm_wanted in nt):
            return i, f"步骤名 {wanted} 已模糊匹配到 {s['key']}"

    idx = next((i for i, s in enumerate(steps) if s["key"] == current_step), 0)
    return idx, (
        f"这个任务没有名为 {wanted} 的步骤,已按当前步骤 {steps[idx]['key']} 处理。"
        f"本任务的合法步骤是:{[s['key'] for s in steps]}"
    )


# ---------------- 任务 ----------------
def create_task(session: Session, task_type: str, title: str, details: dict | None, conversation_id: str | None) -> Task:
    if task_type not in TASK_TYPES:
        raise ValueError(f"未知任务类型 {task_type},只能是 {', '.join(TASK_TYPES)}")
    steps = new_steps(task_type)
    task = Task(type=task_type, title=title[:200], details=details or {}, steps=steps, current_step=steps[0]["key"], conversation_id=conversation_id)
    session.add(task)
    session.flush()
    if conversation_id:
        conv = session.get(Conversation, conversation_id)
        if conv:
            conv.task_id = task.id
            if conv.agent_type in ("auto", "general"):
                conv.agent_type = task_type
    bus.publish({"type": "task_update", "task": task_to_dict(task)})
    return task


def get_task(session: Session, task_id: str) -> Task | None:
    return session.get(Task, task_id)


def list_tasks(session: Session, conversation_id: str | None = None, status: str | None = None) -> list[Task]:
    stmt = select(Task).order_by(Task.updated_at.desc())
    if conversation_id:
        stmt = stmt.where(Task.conversation_id == conversation_id)
    if status:
        stmt = stmt.where(Task.status == status)
    return list(session.scalars(stmt))


def update_task(
    session: Session,
    task_id: str,
    *,
    step_key: str | None = None,
    step_status: str | None = None,
    note: str | None = None,
    details: dict | None = None,
    status: str | None = None,
    title: str | None = None,
) -> Task:
    task = session.get(Task, task_id)
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")
    steps = [dict(s) for s in (task.steps or [])]
    now_iso = clock.iso(clock.now())

    resolve_note: str | None = None
    if step_key:
        idx, resolve_note = resolve_step(steps, step_key, task.current_step)
        if idx is None:
            raise ValueError(f"任务 {task_id} 没有步骤 {step_key},可用:{[s['key'] for s in steps]}")
        if step_status:
            if step_status not in STEP_STATUSES:
                raise ValueError(f"步骤状态只能是 {STEP_STATUSES}")
            steps[idx]["status"] = step_status
            steps[idx]["updated_at"] = now_iso
            if step_status in ("done", "skipped"):
                # 之前的步骤自动视为完成;下一个待办步骤进入进行中
                for prev in steps[:idx]:
                    if prev["status"] in ("pending", "in_progress"):
                        prev["status"] = "done"
                        prev["updated_at"] = now_iso
                nxt = next((s for s in steps[idx + 1 :] if s["status"] == "pending"), None)
                if nxt:
                    nxt["status"] = "in_progress"
                    nxt["updated_at"] = now_iso
                    task.current_step = nxt["key"]
                else:
                    task.current_step = steps[idx]["key"]
                    if steps[-1]["status"] in ("done", "skipped") and task.status not in ("cancelled",):
                        task.status = "done"
            elif step_status == "in_progress":
                for prev in steps[:idx]:
                    if prev["status"] in ("pending", "in_progress"):
                        prev["status"] = "done"
                        prev["updated_at"] = now_iso
                task.current_step = step_key
                if task.status == "done":
                    task.status = "active"
        if note:
            steps[idx]["note"] = note
            steps[idx]["updated_at"] = now_iso
    elif note:
        idx = next((i for i, s in enumerate(steps) if s["key"] == task.current_step), None)
        if idx is not None:
            steps[idx]["note"] = note
            steps[idx]["updated_at"] = now_iso

    task.steps = steps
    if details:
        merged = dict(task.details or {})
        merged.update(details)
        task.details = merged
    if status:
        if status not in TASK_STATUSES:
            raise ValueError(f"任务状态只能是 {TASK_STATUSES}")
        task.status = status
        if status == "done":
            for s in steps:
                if s["status"] in ("pending", "in_progress"):
                    s["status"] = "done"
                    s["updated_at"] = now_iso
            task.steps = steps
            task.current_step = steps[-1]["key"] if steps else None
    if title:
        task.title = title[:200]
    task.updated_at = clock.now()
    session.flush()
    bus.publish({"type": "task_update", "task": task_to_dict(task)})
    # 做过兜底匹配时挂在对象上,由工具层带回给模型,让它下次用对名字
    task._resolve_note = resolve_note  # type: ignore[attr-defined]
    return task


# ---------------- 跟进 ----------------
def schedule_followup(session: Session, task_id: str, conversation_id: str | None, due_at: datetime, message: str, kind: str = "check") -> Followup:
    task = session.get(Task, task_id)
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")
    f = Followup(task_id=task_id, conversation_id=conversation_id or task.conversation_id, due_at=due_at, message=message, kind=kind if kind in ("check", "remind", "escalate") else "check")
    session.add(f)
    if task.status == "active":
        task.status = "waiting"
    session.flush()
    bus.publish({"type": "followup_scheduled", "followup": followup_to_dict(f), "task": task_to_dict(task)})
    return f


def schedule_followup_in(session: Session, task_id: str, conversation_id: str | None, delay_hours: float, message: str, kind: str = "check") -> Followup:
    due = clock.now() + timedelta(hours=max(float(delay_hours), 0.01))
    return schedule_followup(session, task_id, conversation_id, due, message, kind)


def list_followups(session: Session, task_id: str | None = None, conversation_id: str | None = None, status: str | None = None) -> list[Followup]:
    stmt = select(Followup).order_by(Followup.due_at.asc())
    if task_id:
        stmt = stmt.where(Followup.task_id == task_id)
    if conversation_id:
        stmt = stmt.where(Followup.conversation_id == conversation_id)
    if status:
        stmt = stmt.where(Followup.status == status)
    return list(session.scalars(stmt))


def cancel_followups(session: Session, task_id: str) -> int:
    n = 0
    for f in list_followups(session, task_id=task_id, status="scheduled"):
        f.status = "cancelled"
        n += 1
    return n


# ---------------- 材料 ----------------
def save_material(session: Session, task_id: str | None, conversation_id: str | None, title: str, kind: str, content: str) -> Material:
    if task_id and not session.get(Task, task_id):
        raise ValueError(f"任务 {task_id} 不存在")
    m = Material(task_id=task_id, conversation_id=conversation_id, title=title[:200], kind=kind if kind in ("script", "letter", "checklist", "guide", "complaint", "other") else "other", content=content)
    session.add(m)
    session.flush()
    bus.publish({"type": "material_saved", "material": material_to_dict(m)})
    return m


def list_materials(session: Session, task_id: str | None = None, conversation_id: str | None = None) -> list[Material]:
    stmt = select(Material).order_by(Material.created_at.asc())
    if task_id:
        stmt = stmt.where(Material.task_id == task_id)
    if conversation_id:
        stmt = stmt.where(Material.conversation_id == conversation_id)
    return list(session.scalars(stmt))


# ---------------- 给模型看的任务摘要 ----------------
def task_summary_for_prompt(session: Session, task: Task) -> str:
    d = task_to_dict(task)
    lines = [f"任务 {d['id']}(类型 {d['type']} / {d['type_label']}):{d['title']}", f"状态:{d['status']};当前步骤:{d['current_step']}({d['current_step_title']});进度 {d['progress']}%"]
    for s in d["steps"]:
        mark = {"done": "✅", "skipped": "⏭", "in_progress": "▶", "pending": "○"}[s["status"]]
        note = f" —— {s['note']}" if s.get("note") else ""
        lines.append(f"  {mark} {s['key']}:{s['title']}{note}")
    if d["details"]:
        import json

        lines.append("已记录信息:" + json.dumps(d["details"], ensure_ascii=False))
    fus = list_followups(session, task_id=task.id)
    if fus:
        lines.append("跟进计划:")
        for f in fus:
            lines.append(f"  - [{f.status}] {clock.fmt_local(f.due_at)} {f.message}")
    mats = list_materials(session, task_id=task.id)
    if mats:
        lines.append("已生成材料:" + ",".join(f"#{m.id} {m.title}" for m in mats))
    return "\n".join(lines)
