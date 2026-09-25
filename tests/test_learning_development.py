from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import database
import main
import models
import schemas


def _tenant(session, tenant_id, slug):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    tenant = session.get(models.Tenant, tenant_id)
    if tenant is None:
        tenant = models.Tenant(id=tenant_id, name=slug, slug=slug)
        session.add(tenant)
        session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return tenant


def _member(session, tenant_id, email, role="member", name=None):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
    session.add(user); session.flush()
    member = models.Member(tenant_id=tenant_id, user_id=user.id, name=name or email, email=email, role=role)
    session.add(member); session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _seed_learning(session, tenant_id):
    reserved_id = main._unassigned_client_id(tenant_id)
    if session.get(models.Client, reserved_id) is None:
        session.add(models.Client(id=reserved_id, name=main.UNASSIGNED_CLIENT_NAME, code=None))
    session.add(models.TaskTypeOption(name=main.BUILTIN_LEARNING_TASK_TYPE, is_billable=False))
    session.add(models.LearningCategory(name="Tax"))
    session.add(models.LearningCategory(name="VAT"))
    session.commit()


def test_ld_can_start_without_fields_but_completion_requires_learning_details():
    s = database.SessionLocal()
    try:
        tenant_id = "tenant_ld_required"
        _tenant(s, tenant_id, "tenant-ld-required")
        member = _member(s, tenant_id, "learner-required@example.com", name="Learner Required")
        _seed_learning(s, tenant_id)

        task = main.create_task(
            schemas.TaskCreate(client_id="", client_name="", name=main.BUILTIN_LEARNING_TASK_TYPE, task_type=main.BUILTIN_LEARNING_TASK_TYPE),
            current_member=member, db=s,
        )
        started = main.start_task(task.id, schemas.TaskStart(), current_member=member, db=s)
        assert started.status == "running"

        with pytest.raises(HTTPException) as missing:
            main.submit_task(task.id, schemas.TaskSubmit(), current_member=member, db=s)
        assert missing.value.status_code == 400
        assert "Major Category" in missing.value.detail

        submitted = main.submit_task(
            task.id,
            schemas.TaskSubmit(
                learning_category="Tax",
                learning_topic="Close company surcharge",
                what_i_learned="Reviewed when the surcharge applies and the timing of distributions.",
                tdm_references=[schemas.LearningReference(title="TDM note", url="https://example.com/tdm")],
                article_references=[schemas.LearningReference(title="Revenue", url="https://example.com/article")],
            ),
            current_member=member, db=s,
        )
        assert submitted.status == "submitted"
        record = s.query(models.LearningRecord).filter(models.LearningRecord.task_id == task.id).one()
        assert record.member_name == "Learner Required"
        assert record.category == "Tax"
        assert record.topic == "Close company surcharge"
        assert record.duration_seconds >= 0
        assert record.tdm_references[0]["title"] == "TDM note"
    finally:
        s.close()


def test_staff_library_groups_by_person_and_prioritizes_relevance_then_frequency():
    s = database.SessionLocal()
    try:
        tenant_id = "tenant_ld_library"
        _tenant(s, tenant_id, "tenant-ld-library")
        viewer = _member(s, tenant_id, "viewer-ld-library@example.com", name="Viewer")
        a = _member(s, tenant_id, "alex-ld-library@example.com", name="Alex")
        b = _member(s, tenant_id, "bailey-ld-library@example.com", name="Bailey")
        _seed_learning(s, tenant_id)
        now = datetime.utcnow()
        rows = [
            models.LearningRecord(task_id="task_ld_a1", member_id=a.id, member_name=a.name, category="Tax", topic="VAT and tax overview", what_i_learned="VAT filing interaction", duration_seconds=600, learned_at=now - timedelta(days=4)),
            models.LearningRecord(task_id="task_ld_a2", member_id=a.id, member_name=a.name, category="Tax", topic="Tax return", what_i_learned="VAT treatment in a tax return", duration_seconds=600, learned_at=now - timedelta(days=3)),
            models.LearningRecord(task_id="task_ld_b1", member_id=b.id, member_name=b.name, category="VAT", topic="VAT", what_i_learned="VAT rates", duration_seconds=600, learned_at=now - timedelta(days=10)),
        ]
        # LearningRecord task IDs are foreign keys, so create minimal submitted tasks first.
        reserved = main._unassigned_client_id(tenant_id)
        for row in rows:
            s.add(models.TaskInstance(id=row.task_id, client_id=reserved, client_name=main.UNASSIGNED_CLIENT_NAME, name=main.BUILTIN_LEARNING_TASK_TYPE, task_type=main.BUILTIN_LEARNING_TASK_TYPE, owner_id=row.member_id, status="submitted", segments=[]))
        s.flush()
        s.add_all(rows); s.commit()

        result = main.learning_library(keyword="VAT", category="", letter="", current_member=viewer, db=s)
        assert result[0].member_name == "Bailey"  # exact topic relevance wins before frequency
        assert result[1].member_name == "Alex"
        assert result[1].relevant_count == 2
        assert not hasattr(result[0].records[0], "duration_seconds")
    finally:
        s.close()


def test_management_ld_report_respects_admin_scope_and_includes_full_fields():
    s = database.SessionLocal()
    try:
        tenant_id = "tenant_ld_report"
        _tenant(s, tenant_id, "tenant-ld-report")
        admin = _member(s, tenant_id, "admin-ld-report@example.com", role="admin", name="Admin")
        staff = _member(s, tenant_id, "staff-ld-report@example.com", name="Staff")
        _seed_learning(s, tenant_id)
        reserved = main._unassigned_client_id(tenant_id)
        task = models.TaskInstance(client_id=reserved, client_name=main.UNASSIGNED_CLIENT_NAME, name=main.BUILTIN_LEARNING_TASK_TYPE, task_type=main.BUILTIN_LEARNING_TASK_TYPE, owner_id=staff.id, status="submitted", segments=[])
        s.add(task); s.flush()
        s.add(models.LearningRecord(task_id=task.id, member_id=staff.id, member_name=staff.name, category="Tax", topic="Corporation tax", what_i_learned="Learned filing rules", duration_seconds=1800, learned_at=datetime.utcnow(), tdm_references=[], article_references=[]))
        s.commit()

        rows = main.learning_management_report(person_id=staff.id, current_member=admin, db=s)
        assert len(rows) == 1
        assert rows[0].member_name == "Staff"
        assert rows[0].duration_seconds == 1800
        assert rows[0].what_i_learned == "Learned filing rules"
    finally:
        s.close()


def test_learning_categories_are_super_admin_managed_and_history_is_snapshot():
    s = database.SessionLocal()
    try:
        tenant_id = "tenant_ld_category_lifecycle"
        _tenant(s, tenant_id, "tenant-ld-category-lifecycle")
        super_admin = _member(s, tenant_id, "super-ld-category@example.com", role="super_admin", name="Super")
        admin = _member(s, tenant_id, "admin-ld-category@example.com", role="admin", name="Admin")
        staff = _member(s, tenant_id, "staff-ld-category@example.com", name="Staff")
        _seed_learning(s, tenant_id)
        category = s.query(models.LearningCategory).filter(models.LearningCategory.name == "Tax").one()
        reserved = main._unassigned_client_id(tenant_id)
        task = models.TaskInstance(client_id=reserved, client_name=main.UNASSIGNED_CLIENT_NAME, name=main.BUILTIN_LEARNING_TASK_TYPE, task_type=main.BUILTIN_LEARNING_TASK_TYPE, owner_id=staff.id, status="submitted", segments=[])
        s.add(task); s.flush()
        s.add(models.LearningRecord(task_id=task.id, member_id=staff.id, member_name=staff.name, category="Tax", topic="Old tax topic", what_i_learned="Historical snapshot", duration_seconds=60, learned_at=datetime.utcnow(), tdm_references=[], article_references=[]))
        s.commit()

        with pytest.raises(HTTPException) as denied:
            main.update_learning_category(category.id, schemas.LearningCategoryUpdate(name="Tax / Revenue", expected_version=category.version), current_member=admin, db=s)
        assert denied.value.status_code == 403

        updated = main.update_learning_category(category.id, schemas.LearningCategoryUpdate(name="Tax / Revenue", expected_version=category.version), current_member=super_admin, db=s)
        assert updated.name == "Tax / Revenue"
        historical = s.query(models.LearningRecord).filter(models.LearningRecord.task_id == task.id).one()
        assert historical.category == "Tax"

        archived = main.update_learning_category(updated.id, schemas.LearningCategoryUpdate(is_active=False, expected_version=updated.version), current_member=super_admin, db=s)
        assert archived.is_active is False
        active = main.list_learning_categories(include_archived=False, current_member=staff, db=s)
        assert all(c.id != archived.id for c in active)
        all_rows = main.list_learning_categories(include_archived=True, current_member=super_admin, db=s)
        assert any(c.id == archived.id for c in all_rows)
    finally:
        s.close()


def test_learning_library_frequency_breaks_equal_relevance_ties():
    s = database.SessionLocal()
    try:
        tenant_id = "tenant_ld_library_frequency"
        _tenant(s, tenant_id, "tenant-ld-library-frequency")
        viewer = _member(s, tenant_id, "viewer-frequency@example.com", name="Viewer")
        frequent = _member(s, tenant_id, "frequent@example.com", name="Frequent")
        single = _member(s, tenant_id, "single@example.com", name="Single")
        _seed_learning(s, tenant_id)
        reserved = main._unassigned_client_id(tenant_id)
        now = datetime.utcnow()
        specs = [
            ("task_freq_1", frequent, "VAT basics", now - timedelta(days=3)),
            ("task_freq_2", frequent, "VAT filing", now - timedelta(days=2)),
            ("task_freq_3", frequent, "VAT rates", now - timedelta(days=1)),
            ("task_single_1", single, "VAT overview", now),
        ]
        for task_id, member, topic, learned_at in specs:
            s.add(models.TaskInstance(id=task_id, client_id=reserved, client_name=main.UNASSIGNED_CLIENT_NAME, name=main.BUILTIN_LEARNING_TASK_TYPE, task_type=main.BUILTIN_LEARNING_TASK_TYPE, owner_id=member.id, status="submitted", segments=[]))
            s.flush()
            s.add(models.LearningRecord(task_id=task_id, member_id=member.id, member_name=member.name, category="VAT", topic=topic, what_i_learned="VAT learning", duration_seconds=60, learned_at=learned_at, tdm_references=[], article_references=[]))
        s.commit()

        result = main.learning_library(keyword="VAT", category="", letter="", current_member=viewer, db=s)
        assert result[0].member_name == "Frequent"
        assert result[0].relevant_count == 3
        assert result[1].member_name == "Single"
    finally:
        s.close()
