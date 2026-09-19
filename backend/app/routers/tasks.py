"""任务 / 跟进 / 材料 的 REST 接口(前端面板 + 手动操作用)。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .. import export as export_mod
from ..db import SessionLocal
from ..followup import scheduler
from ..models import Followup, Material
from ..tasks import service
from ..tasks.workflows import WORKFLOWS
from .schemas import CreateTaskIn, FollowupIn, MaterialIn, UpdateTaskIn

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/workflows")
def get_workflows():
    return {
        key: {"label": wf["label"], "agent": wf["agent"], "steps": [{"key": k, "title": t, "hint": h} for k, t, h in wf["steps"]]}
        for key, wf in WORKFLOWS.items()
    }


@router.get("/tasks")
def list_tasks(conversation_id: str | None = None, status: str | None = None):
    session = SessionLocal()
    try:
        return [service.task_to_dict(t) for t in service.list_tasks(session, conversation_id, status)]
    finally:
        session.close()


@router.post("/tasks")
def create_task(body: CreateTaskIn):
    session = SessionLocal()
    try:
        task = service.create_task(session, body.task_type, body.title, body.details, body.conversation_id)
        session.commit()
        return service.task_to_dict(task)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        session.close()


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    session = SessionLocal()
    try:
        task = service.get_task(session, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return {
            "task": service.task_to_dict(task),
            "followups": [service.followup_to_dict(f) for f in service.list_followups(session, task_id=task_id)],
            "materials": [service.material_to_dict(m) for m in service.list_materials(session, task_id=task_id)],
        }
    finally:
        session.close()


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, body: UpdateTaskIn):
    session = SessionLocal()
    try:
        task = service.update_task(
            session, task_id,
            step_key=body.step_key, step_status=body.step_status, note=body.note,
            details=body.details, status=body.status, title=body.title,
        )
        session.commit()
        return service.task_to_dict(task)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        session.close()


# ---------------- 跟进 ----------------
@router.get("/followups")
def list_followups(task_id: str | None = None, conversation_id: str | None = None, status: str | None = None):
    session = SessionLocal()
    try:
        return [service.followup_to_dict(f) for f in service.list_followups(session, task_id, conversation_id, status)]
    finally:
        session.close()


@router.post("/followups")
def create_followup(body: FollowupIn):
    session = SessionLocal()
    try:
        f = service.schedule_followup_in(session, body.task_id, body.conversation_id, body.delay_hours, body.message, body.kind)
        session.commit()
        return service.followup_to_dict(f)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        session.close()


@router.delete("/followups/{followup_id}")
def cancel_followup(followup_id: int):
    session = SessionLocal()
    try:
        f = session.get(Followup, followup_id)
        if not f:
            raise HTTPException(404, "跟进不存在")
        f.status = "cancelled"
        session.commit()
        return service.followup_to_dict(f)
    finally:
        session.close()


@router.post("/followups/{followup_id}/fire")
async def fire_followup(followup_id: int):
    """立刻触发某条跟进(演示用,不用等到点)。"""
    from ..agent.runtime import run_followup

    session = SessionLocal()
    try:
        f = session.get(Followup, followup_id)
        if not f:
            raise HTTPException(404, "跟进不存在")
        if f.status != "scheduled":
            raise HTTPException(400, f"该跟进状态为 {f.status},不能触发")
    finally:
        session.close()
    events = await run_followup(followup_id)
    return {"fired": followup_id, "events": len(events)}


@router.post("/followups/scan")
async def scan_followups():
    """立刻扫描一次到期跟进(配合快进时钟演示)。"""
    n = await scheduler.run_once()
    return {"fired": n}


# ---------------- 材料 ----------------
@router.get("/materials")
def list_materials(task_id: str | None = None, conversation_id: str | None = None):
    session = SessionLocal()
    try:
        return [service.material_to_dict(m) for m in service.list_materials(session, task_id, conversation_id)]
    finally:
        session.close()


@router.post("/materials")
def create_material(body: MaterialIn):
    session = SessionLocal()
    try:
        m = service.save_material(session, body.task_id, body.conversation_id, body.title, body.kind, body.content)
        session.commit()
        return service.material_to_dict(m)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        session.close()


@router.get("/materials/{material_id}/export")
def export_material(material_id: int, fmt: str = Query("md", pattern="^(md|txt)$")):
    session = SessionLocal()
    try:
        m = session.get(Material, material_id)
        if not m:
            raise HTTPException(404, "材料不存在")
        content = export_mod.material_markdown(m) if fmt == "md" else f"{m.title}\n\n{m.content}"
        filename = export_mod.safe_filename(m.title) + ("." + fmt)
        return Response(
            content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{__import__('urllib.parse', fromlist=['quote']).quote(filename)}"},
        )
    finally:
        session.close()


@router.get("/tasks/{task_id}/export")
def export_task(task_id: str, fmt: str = Query("md", pattern="^(md|zip)$")):
    session = SessionLocal()
    try:
        task = service.get_task(session, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        quote = __import__("urllib.parse", fromlist=["quote"]).quote
        if fmt == "zip":
            data = export_mod.task_zip(session, task)
            name = export_mod.safe_filename(task.title) + "-全套材料.zip"
            return Response(data, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        md = export_mod.task_dossier_markdown(session, task)
        name = export_mod.safe_filename(task.title) + "-卷宗.md"
        return Response(md.encode("utf-8"), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
    finally:
        session.close()
