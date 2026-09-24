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


def _member(session, tenant_id, email, role="member"):
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
        )
        session.add(member)
        session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _client(session, name="Client", code="C1"):
    client = models.Client(name=name, code=code)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def test_helping_training_requires_other_person_and_stores_it():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_helping_current", "tenant-helping-current")
        owner = _member(s, "tenant_helping_current", "owner-helping@example.com")
        colleague = _member(s, "tenant_helping_current", "colleague-helping@example.com")
        client = _client(s, "Internal", "HELP")
        s.add(models.TaskTypeOption(name=main.BUILTIN_HELPING_TASK_TYPE, is_billable=False))
        s.commit()

        base = dict(
            client_id=client.id,
            client_name=client.name,
            name="Training support",
            task_type=main.BUILTIN_HELPING_TASK_TYPE,
        )
        with pytest.raises(HTTPException) as missing:
            main.create_task(schemas.TaskCreate(**base), current_member=owner, db=s)
        assert missing.value.status_code == 400

        with pytest.raises(HTTPException) as self_help:
            main.create_task(
                schemas.TaskCreate(**base, helped_member_id=owner.id),
                current_member=owner,
                db=s,
            )
        assert self_help.value.status_code == 400

        task = main.create_task(
            schemas.TaskCreate(**base, helped_member_id=colleague.id),
            current_member=owner,
            db=s,
        )
        assert task.helped_member_id == colleague.id
        assert task.task_type == main.BUILTIN_HELPING_TASK_TYPE
    finally:
        s.close()


def test_presence_heartbeat_works_without_timer_and_updates_same_daily_row():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_presence_current", "tenant-presence-current")
        member = _member(s, "tenant_presence_current", "presence-current@example.com", role="super_admin")
        assert main.audit_presence_heartbeat(current_member=member, db=s) is None
        rows = s.query(models.DailyPresenceEvent).filter(models.DailyPresenceEvent.member_id == member.id).all()
        assert len(rows) == 1
        first_seen = rows[0].first_seen_at
        first_last_seen = rows[0].last_seen_at

        assert main.audit_presence_heartbeat(current_member=member, db=s) is None
        rows = s.query(models.DailyPresenceEvent).filter(models.DailyPresenceEvent.member_id == member.id).all()
        assert len(rows) == 1
        assert rows[0].first_seen_at == first_seen
        assert rows[0].last_seen_at >= first_last_seen
    finally:
        s.close()


def test_delete_task_cleans_task_derived_rows_and_detaches_inactivity():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_delete_current", "tenant-delete-current")
        member = _member(s, "tenant_delete_current", "delete-current@example.com", role="super_admin")
        colleague = _member(s, "tenant_delete_current", "delete-colleague@example.com")
        client = _client(s, "Delete client", "DEL")
        task = models.TaskInstance(
            client_id=client.id,
            client_name=client.name,
            name="Delete me",
            owner_id=member.id,
            status="paused",
            segments=[],
            role="",
            task_type="",
        )
        s.add(task)
        s.commit()
        s.refresh(task)

        clock = models.ClockStartEvent(member_id=member.id, task_id=task.id, started_at=datetime.utcnow())
        inactivity = models.InactivityEvent(
            member_id=member.id,
            kind="lock",
            started_at=datetime.utcnow() - timedelta(minutes=2),
            ended_at=datetime.utcnow(),
            seconds=120,
            task_id=task.id,
        )
        help_row = models.HelpEvent(
            member_id=member.id,
            colleague_id=colleague.id,
            direction="given",
            seconds=60,
            task_id=task.id,
        )
        s.add_all([clock, inactivity, help_row])
        s.commit()
        inactivity_id = inactivity.id

        assert main.delete_task(task.id, current_member=member, db=s) is None
        assert s.get(models.TaskInstance, task.id) is None
        assert s.query(models.ClockStartEvent).filter(models.ClockStartEvent.task_id == task.id).count() == 0
        assert s.query(models.HelpEvent).filter(models.HelpEvent.task_id == task.id).count() == 0
        kept = s.get(models.InactivityEvent, inactivity_id)
        assert kept is not None
        assert kept.task_id is None
    finally:
        s.close()


def test_activity_summary_exposes_gross_inactivity_and_net_spans():
    s = database.SessionLocal()
    try:
        _tenant(s, "tenant_activity_current", "tenant-activity-current")
        admin = _member(s, "tenant_activity_current", "activity-current@example.com", role="super_admin")
        now = datetime.utcnow().replace(microsecond=0)
        work_date = (now - timedelta(days=1)).date()
        login_at = datetime.combine(work_date, datetime.min.time()).replace(hour=9)
        last_seen = login_at + timedelta(hours=9)
        s.add(models.LoginEvent(member_id=admin.id, created_at=login_at))
        s.add(models.DailyPresenceEvent(
            member_id=admin.id,
            work_date=work_date,
            first_seen_at=login_at,
            last_seen_at=last_seen,
        ))
        s.add(models.InactivityEvent(
            member_id=admin.id,
            kind="lock",
            started_at=login_at + timedelta(hours=3),
            ended_at=login_at + timedelta(hours=3, minutes=30),
            seconds=1800,
        ))
        s.commit()

        result = main.audit_activity_summary(
            date_from=work_date.isoformat(),
            date_to=work_date.isoformat(),
            current_member=admin,
            db=s,
        )
        rows = result if isinstance(result, list) else result.get("rows", [])
        row = next(r for r in rows if r.get("member_id") == admin.id and r.get("date") == work_date.isoformat())
        assert "first_login_to_shutdown_seconds" in row
        assert "inactivity_seconds" in row
        assert "net_first_login_to_shutdown_seconds" in row
        assert row["first_login_to_shutdown_seconds"] is not None
        assert row["net_first_login_to_shutdown_seconds"] <= row["first_login_to_shutdown_seconds"]
    finally:
        s.close()
