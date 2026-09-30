from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import database
import main
import models
import schemas


def _tenant(session, tenant_id):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    tenant = session.get(models.Tenant, tenant_id)
    if tenant is None:
        tenant = models.Tenant(id=tenant_id, name=tenant_id, slug=tenant_id)
        session.add(tenant)
        session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return tenant


def _member(session, tenant_id, email, role="member", pod_id=None, timezone_name="UTC"):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(
        tenant_id=tenant_id,
        user_id=user.id,
        name=email,
        email=email,
        role=role,
        pod_id=pod_id,
        timezone_name=timezone_name,
        additional_permissions=[],
    )
    session.add(member)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _client(session, name="Client"):
    client = models.Client(name=name, code=name[:3].upper())
    session.add(client)
    session.commit()
    return client


def test_time_integrity_calculation_uses_net_active_presence_at_recorded_at():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_time_integrity_calc")
        member = _member(s, tenant.id, "integrity-calc@example.com")
        s.info["actor_member_id"] = member.id
        client = _client(s, "Calc Client")
        day = datetime(2026, 9, 30)

        s.add(models.ActivePresenceInterval(
            member_id=member.id,
            work_date=day.date(),
            started_at=day.replace(hour=9),
            ended_at=day.replace(hour=15),
        ))
        s.add(models.InactivityEvent(
            member_id=member.id,
            kind="screen_locked",
            started_at=day.replace(hour=12),
            ended_at=day.replace(hour=13),
            seconds=3600,
        ))
        tracked = models.TaskInstance(
            client_id=client.id,
            client_name=client.name,
            name="Tracked work",
            owner_id=member.id,
            status="paused",
            segments=[{"start": day.replace(hour=9).isoformat() + "Z", "end": day.replace(hour=12).isoformat() + "Z"}],
        )
        manual = models.TaskInstance(
            client_id=client.id,
            client_name=client.name,
            name="Manual work",
            owner_id=member.id,
            status="todo",
            segments=[],
            created_at=day.replace(hour=13),
        )
        s.add_all([tracked, manual])
        s.commit()

        row = main._append_time_integrity_snapshot(
            s,
            member=member,
            task=manual,
            entry_source="Raw Manual",
            manual_duration_seconds=3 * 3600,
            current_value_seconds=3 * 3600,
            recorded_at=day.replace(hour=15),
            work_date=day.date(),
            reason_note="Manual entry",
        )
        s.commit()
        s.refresh(row)

        assert row.net_active_presence_seconds == pytest.approx(5 * 3600)
        assert row.automatically_tracked_seconds == pytest.approx(3 * 3600)
        assert row.recovered_allocated_seconds == pytest.approx(0)
        assert row.prior_manual_allocated_seconds == pytest.approx(0)
        assert row.available_unallocated_active_seconds == pytest.approx(2 * 3600)
        assert row.unreconciled_manual_seconds == pytest.approx(1 * 3600)
        assert row.recorded_at == day.replace(hour=15)
    finally:
        s.close()


def test_time_integrity_requires_delegated_permission_and_keeps_historical_pod_scope():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_time_integrity_scope")
        pod_a = models.Pod(name="Pod A")
        pod_b = models.Pod(name="Pod B")
        s.add_all([pod_a, pod_b])
        s.commit()
        super_admin = _member(s, tenant.id, "integrity-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "integrity-admin@example.com", "admin", pod_a.id)
        staff_a = _member(s, tenant.id, "integrity-a@example.com", "member", pod_a.id)
        staff_b = _member(s, tenant.id, "integrity-b@example.com", "member", pod_b.id)
        client = _client(s, "Scope Client")
        now = datetime(2026, 9, 30, 12)

        for person, pod, suffix in [(staff_a, pod_a, "A"), (staff_b, pod_b, "B")]:
            s.add(models.TimeIntegrityAuditEntry(
                entry_group_id=models.gen_id("grp"),
                revision=1,
                event_kind="recorded",
                task_id=None,
                member_id=person.id,
                submitted_pod_id=pod.id,
                work_date=now.date(),
                client_id=client.id,
                client_name=client.name,
                task_name=f"Task {suffix}",
                entry_source="Raw Manual",
                recorded_at=now,
                net_active_presence_seconds=3600,
                automatically_tracked_seconds=0,
                recovered_allocated_seconds=0,
                prior_manual_allocated_seconds=0,
                available_unallocated_active_seconds=1800,
                manual_duration_seconds=3600,
                unreconciled_manual_seconds=1800,
                original_value_seconds=3600,
                current_value_seconds=3600,
                reason_note="",
            ))
        s.commit()

        with pytest.raises(HTTPException) as denied:
            main.time_integrity_audit_report(current_member=admin, db=s)
        assert denied.value.status_code == 403

        admin = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_TIME_INTEGRITY_AUDIT],
                expected_version=admin.version,
            ),
            current_member=super_admin,
            db=s,
        )
        report = main.time_integrity_audit_report(current_member=admin, db=s)
        assert len(report.rows) == 1
        assert report.rows[0].member_id == staff_a.id
        assert report.rows[0].pod_id == pod_a.id

        with pytest.raises(HTTPException) as wrong_pod:
            main.time_integrity_audit_report(pod_id=pod_b.id, current_member=admin, db=s)
        assert wrong_pod.value.status_code == 403
    finally:
        s.close()


def test_time_integrity_tenant_isolation_and_append_only_edit_history():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_time_integrity_history")
        super_admin = _member(s, tenant.id, "history-super@example.com", "super_admin")
        staff = _member(s, tenant.id, "history-staff@example.com")
        s.info["actor_member_id"] = staff.id
        client = _client(s, "History Client")
        day = datetime(2026, 9, 30)
        s.add(models.ActivePresenceInterval(
            member_id=staff.id, work_date=day.date(), started_at=day.replace(hour=9), ended_at=day.replace(hour=17)
        ))
        task = models.TaskInstance(
            client_id=client.id, client_name=client.name, name="History task",
            owner_id=staff.id, status="todo", segments=[], created_at=day.replace(hour=9),
        )
        s.add(task)
        s.commit()

        original = main._append_time_integrity_snapshot(
            s, member=staff, task=task, entry_source="Raw Manual",
            manual_duration_seconds=3600, current_value_seconds=3600,
            recorded_at=day.replace(hour=12), work_date=day.date(), reason_note="Original",
        )
        s.commit()
        group_id = original.entry_group_id
        original_recorded_at = original.recorded_at

        original.recorded_at = day.replace(hour=11)
        with pytest.raises(ValueError, match="append-only"):
            s.commit()
        s.rollback()
        original = s.get(models.TimeIntegrityAuditEntry, original.id)
        assert original.recorded_at == original_recorded_at

        edited = main._append_time_integrity_snapshot(
            s, member=staff, task=task, entry_source="Raw Manual",
            manual_duration_seconds=5400, current_value_seconds=5400,
            recorded_at=day.replace(hour=13), work_date=day.date(), reason_note="Edited",
            entry_group_id=group_id, event_kind="edited",
        )
        s.commit()
        assert edited.revision == 2
        assert edited.id != original.id
        assert original.recorded_at == original_recorded_at

        s.info["actor_member_id"] = super_admin.id
        report = main.time_integrity_audit_report(current_member=super_admin, db=s)
        row = next(r for r in report.rows if r.entry_group_id == group_id)
        assert row.later_edited is True
        assert row.original_value_seconds == pytest.approx(3600)
        assert row.current_value_seconds == pytest.approx(5400)
        assert row.recorded_at == original_recorded_at
        assert row.last_edited_at == day.replace(hour=13)

        # A different tenant's row is invisible even to this tenant's Super Admin.
        s.info["skip_tenant_scope"] = True
        other = models.Tenant(id="tenant_time_integrity_other", name="Other", slug="tenant_time_integrity_other")
        s.add(other)
        s.commit()
        s.info.pop("skip_tenant_scope", None)
        s.info["tenant_id"] = tenant.id
        report_after = main.time_integrity_audit_report(current_member=super_admin, db=s)
        assert all(r.member_id == staff.id for r in report_after.rows)
    finally:
        s.close()


def test_time_integrity_snapshots_selected_timezone_and_supports_multi_filters():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_time_integrity_timezone")
        super_admin = _member(s, tenant.id, "timezone-super@example.com", "super_admin", timezone_name="Asia/Colombo")
        staff_a = _member(s, tenant.id, "timezone-a@example.com", timezone_name="Asia/Colombo")
        staff_b = _member(s, tenant.id, "timezone-b@example.com", timezone_name="Europe/Dublin")
        s.info["actor_member_id"] = super_admin.id
        client = _client(s, "Timezone Client")
        day = datetime(2026, 9, 30)

        for staff, hour, source in [(staff_a, 9, "Raw Manual"), (staff_b, 10, "Recovery")]:
            s.add(models.ActivePresenceInterval(
                member_id=staff.id,
                work_date=day.date(),
                started_at=day.replace(hour=8),
                ended_at=day.replace(hour=12),
            ))
            task = models.TaskInstance(
                client_id=client.id,
                client_name=client.name,
                name=f"Task {staff.name}",
                owner_id=staff.id,
                status="todo",
                segments=[],
                created_at=day.replace(hour=8),
            )
            s.add(task)
            s.flush()
            main._append_time_integrity_snapshot(
                s,
                member=staff,
                task=task,
                entry_source=source,
                manual_duration_seconds=600,
                current_value_seconds=600,
                recorded_at=day.replace(hour=hour),
                work_date=day.date(),
            )
        s.commit()

        first = s.query(models.TimeIntegrityAuditEntry).filter(models.TimeIntegrityAuditEntry.member_id == staff_a.id).first()
        assert first.recorded_timezone_name == "Asia/Colombo"

        # Changing the member profile later must not rewrite the location/timezone captured at RecordedAt.
        staff_a.timezone_name = "America/Toronto"
        s.commit()
        s.refresh(first)
        assert first.recorded_timezone_name == "Asia/Colombo"

        report = main.time_integrity_audit_report(
            member_id=f"{staff_a.id},{staff_b.id}",
            entry_source="Raw Manual,Recovery",
            recorded_location="Asia/Colombo,Europe/Dublin",
            edited="yes,no",
            entry_timing="same_day,later_day",
            current_member=super_admin,
            db=s,
        )
        assert {row.member_id for row in report.rows} == {staff_a.id, staff_b.id}
        by_member = {row.member_id: row for row in report.rows}
        assert by_member[staff_a.id].recorded_timezone_name == "Asia/Colombo"
        assert by_member[staff_b.id].recorded_timezone_name == "Europe/Dublin"
    finally:
        s.close()
