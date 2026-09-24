from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm.exc import StaleDataError

import database
import main
import models
import schemas


def _tenant(session, tenant_id, slug):
    session.info["skip_tenant_scope"] = True
    existing = session.get(models.Tenant, tenant_id)
    if existing:
        session.info.pop("skip_tenant_scope", None)
        return existing
    t = models.Tenant(id=tenant_id, name=slug, slug=slug)
    session.add(t)
    session.commit()
    session.info.pop("skip_tenant_scope", None)
    return t


def _member(session, tenant_id, email, role="member", pod_id=None):
    previous_skip = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = session.query(models.User).filter(models.User.email == email).first()
    if not user:
        user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
        session.add(user)
        session.flush()
    member = session.query(models.Member).filter(
        models.Member.tenant_id == tenant_id, models.Member.user_id == user.id
    ).first()
    if not member:
        member = models.Member(tenant_id=tenant_id, user_id=user.id, name=email, email=email, role=role, pod_id=pod_id)
        session.add(member)
        session.commit()
    if previous_skip is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous_skip
    return member


def _client(session, tenant_id, code):
    session.info["tenant_id"] = tenant_id
    c = models.Client(name=f"Client {code}", code=code)
    session.add(c); session.commit(); session.refresh(c)
    return c


def _task(session, tenant_id, client, owner, *, name="Task", status="todo", segments=None):
    session.info["tenant_id"] = tenant_id
    t = models.TaskInstance(
        client_id=client.id, client_name=client.name, name=name, owner_id=owner.id,
        status=status, segments=list(segments or []), role="", task_type="",
    )
    session.add(t); session.commit(); session.refresh(t)
    return t


def test_request_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        schemas.ClientCreate(name="Client", code="C1", role="super_admin")


def test_high_risk_request_fields_are_bounded():
    with pytest.raises(ValidationError):
        schemas.ClientCreate(name="X" * 241, code="C1")
    with pytest.raises(ValidationError):
        schemas.MemberCapacityUpdate(weekly_capacity_hours=-1, expected_version=1)
    with pytest.raises(ValidationError):
        schemas.QuickMeetingCreate(summary="x", duration_minutes=5000)
    with pytest.raises(ValidationError):
        schemas.ClientImportRequest(rows=[schemas.ClientImportRow(name="A", code="A", bank_accounts=[])] * 5001)


def test_tenant_query_isolation():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_query_a", "tenant-query-a")
        _tenant(s, "tenant_query_b", "tenant-query-b")
        s.info["tenant_id"] = "tenant_query_a"
        s.add(models.Client(name="A client", code="A1")); s.commit()
        s.info["tenant_id"] = "tenant_query_b"
        s.add(models.Client(name="B client", code="B1")); s.commit()
        s.info["tenant_id"] = "tenant_query_a"
        assert [c.name for c in s.query(models.Client).all()] == ["A client"]
    finally:
        s.close()


def test_cross_tenant_write_guard_blocks_insert():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_write_a", "tenant-write-a")
        _tenant(s, "tenant_write_b", "tenant-write-b")
        s.info["tenant_id"] = "tenant_write_a"
        s.add(models.Client(tenant_id="tenant_write_b", name="Bad", code="BAD"))
        with pytest.raises(ValueError, match="Cross-tenant insert blocked"):
            s.flush()
        s.rollback()
    finally:
        s.close()


def test_optimistic_lock_rejects_stale_admin_edit():
    seed = database.SessionLocal()
    try:
        _tenant(seed, "tenant_lock", "tenant-lock")
        seed.info["tenant_id"] = "tenant_lock"
        c = models.Client(name="Original", code="LOCK1")
        seed.add(c); seed.commit(); client_id = c.id
    finally:
        seed.close()

    s1 = database.SessionLocal(); s1.info["tenant_id"] = "tenant_lock"
    s2 = database.SessionLocal(); s2.info["tenant_id"] = "tenant_lock"
    try:
        c1 = s1.get(models.Client, client_id); c2 = s2.get(models.Client, client_id)
        c1.name = "First edit"; s1.commit()
        c2.name = "Second edit"
        with pytest.raises(StaleDataError):
            s2.commit()
    finally:
        s1.close(); s2.close()


def test_authenticated_mutations_create_audit_event_without_secrets():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_audit", "tenant-audit")
        member = _member(s, "tenant_audit", "audit@example.com", "super_admin")
        s.info["tenant_id"] = "tenant_audit"; s.info["actor_member_id"] = member.id
        setting = models.TenantSetting(key="calamari_api_key_encrypted", value="secret-one")
        s.add(setting); s.commit()
        setting.value = "secret-two"; s.commit()
        events = s.query(models.AuditEvent).filter(models.AuditEvent.entity_type == "TenantSetting").all()
        assert events
        blob = str([e.changes for e in events])
        assert "secret-one" not in blob and "secret-two" not in blob
        assert "setting_value_changed" in blob
    finally:
        s.close()


def test_audit_events_are_append_only_in_application_session():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_audit_immutable", "tenant-audit-immutable")
        member = _member(s, "tenant_audit_immutable", "audit2@example.com", "super_admin")
        s.info["tenant_id"] = "tenant_audit_immutable"; s.info["actor_member_id"] = member.id
        c = models.Client(name="Audited", code="AUD2"); s.add(c); s.commit()
        event = s.query(models.AuditEvent).first()
        event.action = "tampered"
        with pytest.raises(ValueError, match="append-only"):
            s.flush()
        s.rollback()
    finally:
        s.close()


def test_staff_cannot_read_or_manage_another_staff_task():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_auth", "tenant-auth")
        a = _member(s, "tenant_auth", "a@example.com", "member")
        b = _member(s, "tenant_auth", "b@example.com", "member")
        c = _client(s, "tenant_auth", "AUTH1")
        t = _task(s, "tenant_auth", c, b)
        s.info["tenant_id"] = "tenant_auth"
        with pytest.raises(HTTPException) as exc:
            main._require_task_in_scope(a, t, s, owner_can_access=True)
        assert exc.value.status_code == 403
        rows = main.list_tasks(current_member=a, db=s)
        assert all(row.owner_id == a.id for row in rows)
    finally:
        s.close()


def test_admin_pod_scope_cannot_be_bypassed_by_id():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_pods", "tenant-pods")
        s.info["tenant_id"] = "tenant_pods"
        p1 = models.Pod(name="P1"); p2 = models.Pod(name="P2"); s.add_all([p1,p2]); s.commit()
        admin = _member(s, "tenant_pods", "admin@example.com", "admin", p1.id)
        outsider = _member(s, "tenant_pods", "outside@example.com", "member", p2.id)
        c = _client(s, "tenant_pods", "POD1"); t = _task(s, "tenant_pods", c, outsider)
        s.info["tenant_id"] = "tenant_pods"
        with pytest.raises(HTTPException) as exc:
            main._require_task_in_scope(admin, t, s, owner_can_access=True)
        assert exc.value.status_code == 403
    finally:
        s.close()


def test_cross_tenant_object_id_is_not_visible():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_obj_a", "tenant-obj-a"); _tenant(s, "tenant_obj_b", "tenant-obj-b")
        a = _member(s, "tenant_obj_a", "obja@example.com", "super_admin")
        b = _member(s, "tenant_obj_b", "objb@example.com", "member")
        cb = _client(s, "tenant_obj_b", "OBJ2"); tb = _task(s, "tenant_obj_b", cb, b)
        task_id = tb.id
    finally:
        s.close()

    check = database.SessionLocal(); check.info["tenant_id"] = "tenant_obj_a"
    try:
        a = check.query(models.Member).filter(models.Member.email == "obja@example.com").first()
        assert check.get(models.TaskInstance, task_id) is None
        with pytest.raises(HTTPException) as exc:
            main._require_task_in_scope(a, None, check)
        assert exc.value.status_code == 404
    finally:
        check.close()


def test_reports_remain_super_admin_only():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_report", "tenant-report")
        staff = _member(s, "tenant_report", "reportstaff@example.com", "member")
        s.info["tenant_id"] = "tenant_report"
        with pytest.raises(HTTPException) as exc:
            main.audit_changes(current_member=staff, db=s)
        assert exc.value.status_code == 403
    finally:
        s.close()


def test_workspace_session_revoke_requires_super_admin():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_revoke", "tenant-revoke")
        staff = _member(s, "tenant_revoke", "revoke@example.com", "member")
        s.info["tenant_id"] = "tenant_revoke"
        with pytest.raises(HTTPException) as exc:
            main.revoke_workspace_sessions(current_member=staff, db=s)
        assert exc.value.status_code == 403
    finally:
        s.close()


def test_request_body_limit_rejects_actual_oversized_body(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 32)
    with TestClient(main.app) as client:
        response = client.post("/api/does-not-exist", content=b"x" * 33)
    assert response.status_code == 413


def test_integration_configuration_revision_rejects_stale_save():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_integration_lock", "tenant-integration-lock")
        s.info["tenant_id"] = "tenant_integration_lock"
        current = main._integration_revision(s, "karbon")
        assert current == 0
        assert main._require_integration_revision(s, "karbon", None, False) == 0
        updated = main._bump_integration_revision(s, "karbon", current)
        s.commit()
        assert updated == 1
        with pytest.raises(HTTPException) as exc:
            main._require_integration_revision(s, "karbon", 0, True)
        assert exc.value.status_code == 409
        assert main._require_integration_revision(s, "karbon", 1, True) == 1
    finally:
        s.close()
