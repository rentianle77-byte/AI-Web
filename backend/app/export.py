"""材料导出:单份 Markdown / 整个任务的完整卷宗。"""
from __future__ import annotations

import io
import zipfile

from sqlalchemy.orm import Session

from . import clock
from .models import Material, Task
from .tasks import service

KIND_LABEL = {"script": "话术", "letter": "申请书", "checklist": "清单", "guide": "指引", "complaint": "申诉材料", "other": "材料"}


def material_markdown(m: Material) -> str:
    return f"# {m.title}\n\n> 类型:{KIND_LABEL.get(m.kind, m.kind)} · 生成时间:{clock.fmt_local(m.created_at)}\n\n{m.content}\n"


def safe_filename(name: str) -> str:
    bad = '/\\:*?"<>|\n\r\t'
    cleaned = "".join("_" if ch in bad else ch for ch in name).strip() or "material"
    return cleaned[:80]


def task_dossier_markdown(session: Session, task: Task) -> str:
    """把一个任务的全部过程整理成一份可直接打印/提交的卷宗。"""
    d = service.task_to_dict(task)
    lines = [
        f"# {d['title']}",
        "",
        f"- 任务编号:`{d['id']}`",
        f"- 任务类型:{d['type_label']}",
        f"- 当前状态:{d['status']} · 进度 {d['progress']}%",
        f"- 创建时间:{clock.fmt_local(task.created_at)}",
        f"- 最近更新:{clock.fmt_local(task.updated_at)}",
        "",
        "## 一、案情要素",
        "",
    ]
    details = d.get("details") or {}
    if details:
        lines += ["| 项目 | 内容 |", "| --- | --- |"]
        for k, v in details.items():
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("(暂无记录)")
    lines += ["", "## 二、办理过程", ""]
    for i, s in enumerate(d["steps"], 1):
        mark = {"done": "已完成", "skipped": "已跳过", "in_progress": "进行中", "pending": "待处理"}[s["status"]]
        lines.append(f"{i}. **{s['title']}** —— {mark}")
        if s.get("note"):
            lines.append(f"   - {s['note']}")
        if s.get("updated_at"):
            lines.append(f"   - 更新于 {s['updated_at'][:16].replace('T', ' ')}")
    fus = service.list_followups(session, task_id=task.id)
    if fus:
        lines += ["", "## 三、跟进记录", "", "| 时间 | 类型 | 状态 | 内容 |", "| --- | --- | --- | --- |"]
        kind_label = {"check": "查进展", "remind": "提醒", "escalate": "升级"}
        status_label = {"scheduled": "待触发", "fired": "已触发", "acked": "已处理", "cancelled": "已取消"}
        for f in fus:
            lines.append(f"| {clock.fmt_local(f.due_at)} | {kind_label.get(f.kind, f.kind)} | {status_label.get(f.status, f.status)} | {f.message} |")
    mats = service.list_materials(session, task_id=task.id)
    if mats:
        lines += ["", "## 四、材料附件", ""]
        for i, m in enumerate(mats, 1):
            lines += [f"### 附件 {i}:{m.title}", "", f"> {KIND_LABEL.get(m.kind, m.kind)} · {clock.fmt_local(m.created_at)}", "", m.content, ""]
    lines += ["", "---", "", f"本卷宗由「快递管家 Agent」于 {clock.fmt_local(clock.now())} 自动生成。", "文中法律依据引自公开法规,具体适用请以最新官方文本为准。"]
    return "\n".join(lines)


def task_zip(session: Session, task: Task) -> bytes:
    """卷宗 + 每份材料单独成文件,打包成 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{safe_filename(task.title)}-卷宗.md", task_dossier_markdown(session, task))
        for i, m in enumerate(service.list_materials(session, task_id=task.id), 1):
            zf.writestr(f"材料/{i:02d}-{safe_filename(m.title)}.md", material_markdown(m))
    return buf.getvalue()
