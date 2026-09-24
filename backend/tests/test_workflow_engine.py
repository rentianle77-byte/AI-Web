"""任务状态机 + 跟进调度 + 导出:产品最难的一块(五星难度),行为必须可预期。"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import clock
from app.db import Base
from app.export import task_dossier_markdown, task_zip
from app.tasks import service
from app.tasks.workflows import WORKFLOWS


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    s = maker()
    clock.set_offset(0)
    yield s
    s.close()


def make_task(session, ttype="claim"):
    return service.create_task(session, ttype, f"测试{ttype}", {"快递公司": "中通"}, None)


def test_new_task_starts_at_first_step(session):
    t = make_task(session)
    assert t.status == "active"
    assert t.current_step == WORKFLOWS["claim"]["steps"][0][0]
    assert t.steps[0]["status"] == "in_progress"
    assert all(s["status"] == "pending" for s in t.steps[1:])


def test_completing_step_advances_to_next(session):
    t = make_task(session)
    t = service.update_task(session, t.id, step_key="collect_info", step_status="done", note="信息齐了")
    assert t.steps[0]["status"] == "done"
    assert t.steps[0]["note"] == "信息齐了"
    assert t.steps[1]["status"] == "in_progress"
    assert t.current_step == t.steps[1]["key"]


def test_jumping_ahead_marks_skipped_steps_done(session):
    """模型直接跳到第 4 步时,前面的步骤要自动补成完成,不能留空洞。"""
    t = make_task(session)
    t = service.update_task(session, t.id, step_key="prepare_materials", step_status="done")
    keys = [s["key"] for s in t.steps]
    idx = keys.index("prepare_materials")
    assert all(s["status"] == "done" for s in t.steps[: idx + 1])
    assert t.steps[idx + 1]["status"] == "in_progress"


def test_finishing_last_step_completes_task(session):
    t = make_task(session)
    t = service.update_task(session, t.id, step_key="resolved", step_status="done")
    assert t.status == "done"
    assert service.task_to_dict(t)["progress"] == 100


def test_wrong_workflow_step_key_falls_back(session):
    """模型把寄件任务的步骤名用到索赔任务上时,不该打断流程。

    三类任务的步骤命名各不相同,模型偶尔串台。以前这里直接抛错,
    工具轨迹上就会留下红叉(测试报告 FT-02 观察到连续 3 次失败)。
    现在改为兜底到当前步骤,并把合法步骤名回给模型。
    """
    t = make_task(session, "claim")
    updated = service.update_task(session, t.id, step_key="compare_plans", step_status="done", note="记一笔")
    note = getattr(updated, "_resolve_note", None)
    assert note and "compare_plans" in note
    assert "collect_info" in note  # 提示里要带上合法步骤名
    assert updated.status != "cancelled"


def test_step_key_matched_by_title(session):
    t = make_task(session, "claim")
    updated = service.update_task(session, t.id, step_key="核实物流", step_status="done")
    idx = next(i for i, s in enumerate(updated.steps) if s["key"] == "verify_tracking")
    assert updated.steps[idx]["status"] == "done"


def test_step_key_matched_by_alias(session):
    """模型传 query_tracking(工具名)而不是 verify_tracking(步骤名)也认。"""
    t = make_task(session, "claim")
    updated = service.update_task(session, t.id, step_key="query_tracking", step_status="done")
    idx = next(i for i, s in enumerate(updated.steps) if s["key"] == "verify_tracking")
    assert updated.steps[idx]["status"] == "done"


def test_exact_step_key_has_no_note(session):
    t = make_task(session, "claim")
    updated = service.update_task(session, t.id, step_key="collect_info", step_status="done")
    assert getattr(updated, "_resolve_note", None) is None


def test_details_merge_not_replace(session):
    t = make_task(session)
    t = service.update_task(session, t.id, details={"单号": "731"})
    assert t.details["快递公司"] == "中通"
    assert t.details["单号"] == "731"


def test_progress_counts_done_and_skipped(session):
    t = make_task(session, "ship")
    total = len(t.steps)
    t = service.update_task(session, t.id, step_key="collect_requirements", step_status="done")
    t = service.update_task(session, t.id, step_key="compare_plans", step_status="skipped")
    assert service.task_to_dict(t)["progress"] == round(2 / total * 100)


# ---------------- 跟进 ----------------
def test_scheduling_followup_sets_task_waiting(session):
    t = make_task(session)
    f = service.schedule_followup_in(session, t.id, None, 72, "问问商家签收没")
    assert f.status == "scheduled"
    assert session.get(type(t), t.id).status == "waiting"


def test_followup_due_only_after_clock_advances(session):
    from app.followup.scheduler import due_followup_ids

    t = make_task(session)
    service.schedule_followup_in(session, t.id, None, 72, "第三天问签收")
    session.commit()
    # 用 session 内查询模拟(scheduler 用独立 session,这里直接比时间)
    due_now = [f for f in service.list_followups(session, task_id=t.id) if f.due_at <= clock.now()]
    assert due_now == []
    clock.advance(73)
    due_after = [f for f in service.list_followups(session, task_id=t.id) if f.due_at <= clock.now()]
    assert len(due_after) == 1
    clock.set_offset(0)


def test_cancel_followups_only_scheduled(session):
    t = make_task(session)
    a = service.schedule_followup_in(session, t.id, None, 24, "A")
    b = service.schedule_followup_in(session, t.id, None, 48, "B")
    b.status = "fired"
    session.flush()
    assert service.cancel_followups(session, t.id) == 1
    assert session.get(type(a), a.id).status == "cancelled"


def test_followup_on_missing_task_rejected(session):
    with pytest.raises(ValueError):
        service.schedule_followup_in(session, "T-NOPE", None, 24, "x")


# ---------------- 材料与导出 ----------------
def test_material_saved_and_listed(session):
    t = make_task(session)
    service.save_material(session, t.id, None, "客服话术", "script", "您好,单号 731...")
    mats = service.list_materials(session, task_id=t.id)
    assert len(mats) == 1 and mats[0].kind == "script"


def test_dossier_contains_steps_followups_materials(session):
    t = make_task(session)
    service.update_task(session, t.id, step_key="collect_info", step_status="done", note="已收集")
    service.schedule_followup_in(session, t.id, None, 72, "问进展")
    service.save_material(session, t.id, None, "索赔话术", "script", "正文内容")
    md = task_dossier_markdown(session, t)
    for expect in ("案情要素", "办理过程", "跟进记录", "材料附件", "已收集", "问进展", "正文内容", t.id):
        assert expect in md


def test_task_zip_has_dossier_and_materials(session):
    import io, zipfile

    t = make_task(session)
    service.save_material(session, t.id, None, "话术 A", "script", "内容")
    names = zipfile.ZipFile(io.BytesIO(task_zip(session, t))).namelist()
    assert any("卷宗" in n for n in names)
    assert any(n.startswith("材料/") for n in names)


def test_task_summary_for_prompt_is_readable(session):
    t = make_task(session)
    service.update_task(session, t.id, step_key="collect_info", step_status="done", note="信息齐了")
    service.schedule_followup_in(session, t.id, None, 72, "问进展")
    text = service.task_summary_for_prompt(session, t)
    assert "当前步骤" in text and "信息齐了" in text and "跟进计划" in text
