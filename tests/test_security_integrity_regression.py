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


def _member(session, tenant_id, email, role="member", pod_id=None):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = session.query(models.User).filter(models.User.email == email).first()
    if user is None:
        user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
        session.add(user)
        session.flush()
    member = session.query(models.Member).filter(
        models.Member.tenant_id == tenant_id,
        models.Member.user_id == user.id,
    ).first()
    if member is None:
        member = models.Member(
            tenant_id=tenant_id,
            user_id=user.id,
            name=email,
            email=email,
            role=role,
            pod_id=pod_id,
        )
        session.add(member)
        session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _client(session, name, code):
    client = models.Client(name=name, code=code)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def _submitted_task(session, owner, client, name, *, seconds=600, submitted_pod_id=None, adjusted_seconds=None):
    start = datetime(2026, 9, 25, 9, 0, 0)
    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name=name,
        owner_id=owner.id,
        status="submitted",
        submitted_by_id=owner.id,
        submitted_pod_id=submitted_pod_id,
        submitted_at=start + timedelta(seconds=seconds),
        segments=[{"start": start.isoformat() + "Z", "end": (start + timedelta(seconds=seconds)).isoformat() + "Z"}],
        adjusted_seconds=adjusted_seconds,
        role="",
        task_type="",
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def test_export_never_crosses_tenant_boundary():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_export_a", "phase1-export-a")
        admin_a = _member(s, "phase1_export_a", "phase1-export-a@example.com", "super_admin")
        client_a = _client(s, "Tenant A Client", "P1EA")
        task_a = _submitted_task(s, admin_a, client_a, "Tenant A Task")

        _tenant(s, "phase1_export_b", "phase1-export-b")
        admin_b = _member(s, "phase1_export_b", "phase1-export-b@example.com", "super_admin")
        client_b = _client(s, "Tenant B Client", "P1EB")
        task_b = _submitted_task(s, admin_b, client_b, "Tenant B Task")

        s.info["tenant_id"] = "phase1_export_a"
        admin_a = s.query(models.Member).filter(models.Member.email == "phase1-export-a@example.com").first()
        rows = main.get_export(pushed="all", current_member=admin_a, db=s)
        ids = {row["id"] for row in rows}
        assert task_a.id in ids
        assert task_b.id not in ids
    finally:
        s.close()


def test_export_permissions_member_own_only_and_admin_pod_only():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_export_scope", "phase1-export-scope")
        pod_one = models.Pod(name="Phase 1 Pod One")
        pod_two = models.Pod(name="Phase 1 Pod Two")
        s.add_all([pod_one, pod_two])
        s.commit()

        admin = _member(s, "phase1_export_scope", "phase1-pod-admin@example.com", "admin", pod_one.id)
        own_member = _member(s, "phase1_export_scope", "phase1-own@example.com", "member", pod_one.id)
        other_member = _member(s, "phase1_export_scope", "phase1-other@example.com", "member", pod_two.id)
        super_admin = _member(s, "phase1_export_scope", "phase1-super@example.com", "super_admin", pod_one.id)
        client = _client(s, "Scoped Client", "P1SC")

        own_task = _submitted_task(s, own_member, client, "Own", submitted_pod_id=pod_one.id)
        other_task = _submitted_task(s, other_member, client, "Other pod", submitted_pod_id=pod_two.id)
        super_task = _submitted_task(s, super_admin, client, "Super admin", submitted_pod_id=pod_one.id)

        own_rows = main.get_export(pushed="all", current_member=own_member, db=s)
        assert {row["id"] for row in own_rows} == {own_task.id}

        admin_rows = main.get_export(pushed="all", current_member=admin, db=s)
        admin_ids = {row["id"] for row in admin_rows}
        assert own_task.id in admin_ids
        assert other_task.id not in admin_ids
        assert super_task.id not in admin_ids
    finally:
        s.close()


def test_forgotten_time_recovery_cannot_cross_owner_or_overlap_existing_time():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_recovery", "phase1-recovery")
        a = _member(s, "phase1_recovery", "phase1-recovery-a@example.com")
        b = _member(s, "phase1_recovery", "phase1-recovery-b@example.com")
        client = _client(s, "Recovery Scope", "P1RC")

        task_b = models.TaskInstance(
            client_id=client.id,
            client_name=client.name,
            name="B task",
            owner_id=b.id,
            status="todo",
            segments=[],
            role="",
            task_type="",
        )
        s.add(task_b)
        s.commit()
        with pytest.raises(HTTPException) as cross_owner:
            main.recover_task_time(task_b.id, schemas.TaskRecoverTime(seconds=300), current_member=a, db=s)
        assert cross_owner.value.status_code == 403

        now = datetime.utcnow()
        task_a = models.TaskInstance(
            client_id=client.id,
            client_name=client.name,
            name="A task",
            owner_id=a.id,
            status="paused",
            segments=[{"start": (now - timedelta(minutes=4)).isoformat() + "Z", "end": now.isoformat() + "Z"}],
            role="",
            task_type="",
        )
        s.add(task_a)
        s.commit()
        with pytest.raises(HTTPException) as overlap:
            main.recover_task_time(task_a.id, schemas.TaskRecoverTime(seconds=600), current_member=a, db=s)
        assert overlap.value.status_code == 409
    finally:
        s.close()


def test_manual_adjustment_stays_explicit_in_export_reporting():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_adjustment", "phase1-adjustment")
        member = _member(s, "phase1_adjustment", "phase1-adjustment@example.com", "super_admin")
        client = _client(s, "Adjustment Client", "P1AD")
        task = _submitted_task(s, member, client, "Adjusted task", seconds=600, adjusted_seconds=900)

        rows = main.get_export(pushed="all", current_member=member, db=s)
        row = next(item for item in rows if item["id"] == task.id)
        assert row["adjusted"] is True
        assert row["adjustment_type"] == "Manual override"
        assert row["tracked_seconds"] == 600
        assert row["seconds"] == 900
    finally:
        s.close()


def test_role_downgrade_revokes_existing_sessions_and_admin_cannot_grant_super_admin():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_roles", "phase1-roles")
        super_admin = _member(s, "phase1_roles", "phase1-role-super@example.com", "super_admin")
        admin = _member(s, "phase1_roles", "phase1-role-admin@example.com", "admin")
        target = _member(s, "phase1_roles", "phase1-role-target@example.com", "admin")

        s.add(models.Session(token="phase1-target-session", member_id=target.id, user_id=target.user_id))
        s.commit()
        target_version = target.version
        updated = main.update_member_role(
            target.id,
            schemas.MemberRoleUpdate(role="member", expected_version=target_version),
            current_member=super_admin,
            db=s,
        )
        assert updated.role == "member"
        assert s.query(models.Session).filter(models.Session.member_id == target.id).count() == 0

        admin_version = admin.version
        with pytest.raises(HTTPException) as escalate:
            main.update_member_role(
                admin.id,
                schemas.MemberRoleUpdate(role="super_admin", expected_version=admin_version),
                current_member=admin,
                db=s,
            )
        assert escalate.value.status_code == 403
    finally:
        s.close()


def test_client_endpoint_rejects_stale_expected_version():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_stale", "phase1-stale")
        admin = _member(s, "phase1_stale", "phase1-stale-admin@example.com", "super_admin")
        client = _client(s, "Before", "P1ST")
        stale_version = client.version

        first = main.update_client(
            client.id,
            schemas.ClientCreate(name="First update", code="P1ST", expected_version=stale_version),
            current_member=admin,
            db=s,
        )
        assert first.name == "First update"

        with pytest.raises(HTTPException) as stale:
            main.update_client(
                client.id,
                schemas.ClientCreate(name="Stale overwrite", code="P1ST", expected_version=stale_version),
                current_member=admin,
                db=s,
            )
        assert stale.value.status_code == 409
    finally:
        s.close()


def test_backend_rejects_direct_privileged_calls_from_ordinary_member():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_api_auth", "phase1-api-auth")
        staff = _member(s, "phase1_api_auth", "phase1-api-staff@example.com", "member")
        client = _client(s, "Protected Client", "P1PA")

        with pytest.raises(HTTPException) as client_edit:
            main.update_client(
                client.id,
                schemas.ClientCreate(name="Should not work", code="P1PA", expected_version=client.version),
                current_member=staff,
                db=s,
            )
        assert client_edit.value.status_code == 403

        with pytest.raises(HTTPException) as report_access:
            main.learning_management_report(current_member=staff, db=s)
        assert report_access.value.status_code == 403
    finally:
        s.close()


def test_forgotten_time_recovery_remains_separate_from_automatic_tracked_time_in_export():
    s = database.SessionLocal()
    try:
        _tenant(s, "phase1_recovery_export", "phase1-recovery-export")
        member = _member(s, "phase1_recovery_export", "phase1-recovery-export@example.com", "super_admin")
        client = _client(s, "Recovery Export", "P1RE")
        task = models.TaskInstance(
            client_id=client.id,
            client_name=client.name,
            name="Recovery export task",
            owner_id=member.id,
            status="paused",
            segments=[],
            role="",
            task_type="",
        )
        s.add(task)
        s.commit()
        recovered = main.recover_task_time(task.id, schemas.TaskRecoverTime(seconds=300), current_member=member, db=s)
        recovered.status = "submitted"
        recovered.submitted_by_id = member.id
        recovered.submitted_at = datetime.utcnow()
        s.commit()

        rows = main.get_export(pushed="all", current_member=member, db=s)
        row = next(item for item in rows if item["id"] == task.id)
        assert row["adjusted"] is True
        assert row["adjustment_type"] == "Forgotten time recovered"
        assert row["forgotten_time_recovered_seconds"] == pytest.approx(300, abs=1)
        assert row["tracked_seconds"] == pytest.approx(0, abs=1)
        assert row["seconds"] == pytest.approx(300, abs=1)
    finally:
        s.close()
