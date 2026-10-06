from datetime import datetime

import database
import main
import models


def test_split_forgotten_time_recovery_is_contiguous_audited_and_atomic():
    tenant_id = "tenant_split_recovery"
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant = db.get(models.Tenant, tenant_id)
        if tenant is None:
            tenant = models.Tenant(id=tenant_id, name="Split recovery", slug=tenant_id)
            db.add(tenant)
            db.commit()
        user = models.User(email="split-recovery@example.com", password_hash="x", default_tenant_id=tenant_id)
        db.add(user)
        db.flush()
        member = models.Member(
            tenant_id=tenant_id,
            user_id=user.id,
            name="Split Recovery",
            email=user.email,
            role="member",
            timezone_name="Asia/Colombo",
        )
        db.add(member)
        db.flush()
        client = models.Client(tenant_id=tenant_id, name="Client", code="SPLIT")
        db.add(client)
        db.flush()
        first = models.TaskInstance(
            tenant_id=tenant_id,
            client_id=client.id,
            client_name=client.name,
            name="First recovered task",
            owner_id=member.id,
            status="todo",
            segments=[],
            created_at=datetime.utcnow(),
        )
        second = models.TaskInstance(
            tenant_id=tenant_id,
            client_id=client.id,
            client_name=client.name,
            name="Second recovered task",
            owner_id=member.id,
            status="todo",
            segments=[],
            created_at=datetime.utcnow(),
        )
        db.add_all([first, second])
        db.commit()
        first_id, second_id = first.id, second.id

        db.info.pop("skip_tenant_scope", None)
        db.info["tenant_id"] = tenant_id
        updated = main.recover_task_time_batch(
            {
                "total_seconds": 900,
                "allocations": [
                    {"task_id": first_id, "seconds": 600},
                    {"task_id": second_id, "seconds": 300},
                ],
            },
            current_member=member,
            db=db,
        )

        assert [t.id for t in updated] == [first_id, second_id]
        a = db.get(models.TaskInstance, first_id)
        b = db.get(models.TaskInstance, second_id)
        assert a.status == "paused"
        assert b.status == "paused"
        assert a.segments[-1]["source"] == "forgotten_time_recovery"
        assert b.segments[-1]["source"] == "forgotten_time_recovery"
        assert a.segments[-1]["recovery_batch_id"] == b.segments[-1]["recovery_batch_id"]
        assert a.segments[-1]["end"] == b.segments[-1]["start"]
        assert a.segments[-1]["recovered_seconds"] == 600
        assert b.segments[-1]["recovered_seconds"] == 300

        events = db.query(models.AuditEvent).filter(
            models.AuditEvent.actor_member_id == member.id,
            models.AuditEvent.action == "forgotten_time_recovered",
        ).all()
        batch_events = [e for e in events if (e.changes or {}).get("recovery_batch_id") == a.segments[-1]["recovery_batch_id"]]
        assert len(batch_events) == 2
    finally:
        db.close()


def test_recovery_window_can_freeze_before_later_away_period():
    """Submitting after unlock must keep recovery before the lock, not slide it across away time."""
    from datetime import timedelta

    tenant_id = "tenant_recovery_freeze"
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant = models.Tenant(id=tenant_id, name="Recovery freeze", slug=tenant_id)
        db.add(tenant)
        user = models.User(email="recovery-freeze@example.com", password_hash="x", default_tenant_id=tenant_id)
        db.add(user)
        db.flush()
        member = models.Member(
            tenant_id=tenant_id, user_id=user.id, name="Recovery Freeze",
            email=user.email, role="member", timezone_name="Asia/Colombo",
        )
        db.add(member)
        db.flush()
        client = models.Client(tenant_id=tenant_id, name="Internal", code="FREEZE")
        db.add(client)
        db.flush()
        task = models.TaskInstance(
            tenant_id=tenant_id, client_id=client.id, client_name=client.name, name="Frozen recovery",
            owner_id=member.id, status="todo", segments=[], created_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()
        task_id = task.id

        window_end = datetime.utcnow() - timedelta(minutes=5)
        db.add(models.InactivityEvent(
            tenant_id=tenant_id, member_id=member.id, kind="screen_locked",
            started_at=window_end, ended_at=window_end + timedelta(minutes=5),
            seconds=300, task_id=None,
        ))
        db.commit()

        db.info.pop("skip_tenant_scope", None)
        db.info["tenant_id"] = tenant_id
        updated = main.recover_task_time_batch(
            {
                "total_seconds": 600,
                "window_end_at": window_end.isoformat() + "Z",
                "allocations": [{"task_id": task_id, "seconds": 600}],
            },
            current_member=member,
            db=db,
        )

        segment = updated[0].segments[-1]
        assert segment["source"] == "forgotten_time_recovery"
        assert abs((main.parse_utc_naive(segment["end"]) - window_end).total_seconds()) < 0.01
    finally:
        db.close()


def test_recovery_rejects_overlap_with_recorded_away_time():
    from datetime import timedelta
    from fastapi import HTTPException

    tenant_id = "tenant_recovery_away_overlap"
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant = models.Tenant(id=tenant_id, name="Recovery overlap", slug=tenant_id)
        db.add(tenant)
        user = models.User(email="recovery-overlap@example.com", password_hash="x", default_tenant_id=tenant_id)
        db.add(user)
        db.flush()
        member = models.Member(
            tenant_id=tenant_id, user_id=user.id, name="Recovery Overlap",
            email=user.email, role="member", timezone_name="Asia/Colombo",
        )
        db.add(member)
        db.flush()
        client = models.Client(tenant_id=tenant_id, name="Internal", code="OVERLAP")
        db.add(client)
        db.flush()
        task = models.TaskInstance(
            tenant_id=tenant_id, client_id=client.id, client_name=client.name, name="Overlap recovery",
            owner_id=member.id, status="todo", segments=[], created_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()
        task_id = task.id

        window_end = datetime.utcnow()
        db.add(models.InactivityEvent(
            tenant_id=tenant_id, member_id=member.id, kind="screen_locked",
            started_at=window_end - timedelta(minutes=4),
            ended_at=window_end - timedelta(minutes=2),
            seconds=120, task_id=None,
        ))
        db.commit()

        db.info.pop("skip_tenant_scope", None)
        db.info["tenant_id"] = tenant_id
        try:
            main.recover_task_time_batch(
                {
                    "total_seconds": 600,
                    "window_end_at": window_end.isoformat() + "Z",
                    "allocations": [{"task_id": task_id, "seconds": 600}],
                },
                current_member=member,
                db=db,
            )
            assert False, "Expected away overlap to be rejected"
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "away time" in exc.detail.lower()
    finally:
        db.close()


def test_recovery_can_resume_after_away_without_counting_away_time():
    """One recovery pot may span multiple active slices, but never the away interval between them."""
    from datetime import timedelta

    tenant_id = "tenant_recovery_resume"
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant = models.Tenant(id=tenant_id, name="Recovery resume", slug=tenant_id)
        db.add(tenant)
        user = models.User(email="recovery-resume@example.com", password_hash="x", default_tenant_id=tenant_id)
        db.add(user)
        db.flush()
        member = models.Member(
            tenant_id=tenant_id, user_id=user.id, name="Recovery Resume",
            email=user.email, role="member", timezone_name="Asia/Colombo",
        )
        db.add(member)
        db.flush()
        client = models.Client(tenant_id=tenant_id, name="Internal", code="RESUME")
        db.add(client)
        db.flush()
        task = models.TaskInstance(
            tenant_id=tenant_id, client_id=client.id, client_name=client.name, name="Resumed recovery",
            owner_id=member.id, status="todo", segments=[], created_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()
        task_id = task.id

        now = datetime.utcnow()
        first_start = now - timedelta(minutes=20)
        first_end = now - timedelta(minutes=8)   # 12 active minutes
        away_end = now - timedelta(minutes=3)   # 5 away minutes
        second_end = now                         # 3 more active minutes

        db.add(models.InactivityEvent(
            tenant_id=tenant_id, member_id=member.id, kind="screen_locked",
            started_at=first_end, ended_at=away_end,
            seconds=300, task_id=None,
        ))
        db.commit()

        db.info.pop("skip_tenant_scope", None)
        db.info["tenant_id"] = tenant_id
        updated = main.recover_task_time_batch(
            {
                "total_seconds": 900,
                "window_end_at": second_end.isoformat() + "Z",
                "recovery_windows": [
                    {"start": first_start.isoformat() + "Z", "end": first_end.isoformat() + "Z"},
                    {"start": away_end.isoformat() + "Z", "end": second_end.isoformat() + "Z"},
                ],
                "allocations": [{"task_id": task_id, "seconds": 900}],
            },
            current_member=member,
            db=db,
        )

        recovery_segments = [s for s in updated[0].segments if s.get("source") == "forgotten_time_recovery"]
        assert len(recovery_segments) == 2
        assert round(sum(float(s.get("recovered_seconds") or 0) for s in recovery_segments)) == 900
        assert main.parse_utc_naive(recovery_segments[0]["end"]) <= first_end
        assert main.parse_utc_naive(recovery_segments[1]["start"]) >= away_end
        # The 5-minute lock interval is not part of either recovered segment.
        for seg in recovery_segments:
            seg_start = main.parse_utc_naive(seg["start"])
            seg_end = main.parse_utc_naive(seg["end"])
            assert not (seg_start < away_end and seg_end > first_end)
    finally:
        db.close()


def test_recovery_boundary_round_trip_does_not_false_overlap_server_microseconds():
    """A JS millisecond timestamp representing an exact server segment end must not false-overlap."""
    from datetime import timedelta

    tenant_id = "tenant_recovery_ms_boundary"
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant = models.Tenant(id=tenant_id, name="Recovery ms boundary", slug=tenant_id)
        db.add(tenant)
        user = models.User(email="recovery-ms@example.com", password_hash="x", default_tenant_id=tenant_id)
        db.add(user)
        db.flush()
        member = models.Member(
            tenant_id=tenant_id, user_id=user.id, name="Recovery Millisecond Boundary",
            email=user.email, role="member", timezone_name="Asia/Colombo",
        )
        db.add(member)
        db.flush()
        client = models.Client(tenant_id=tenant_id, name="Internal", code="MSBOUND")
        db.add(client)
        db.flush()

        # Simulate a server-created segment end with microsecond precision. A browser Date
        # round-trip truncates this to milliseconds (.123000).
        exact_end = datetime.utcnow().replace(microsecond=123456) - timedelta(minutes=11)
        browser_end = exact_end.replace(microsecond=123000)
        tracked = models.TaskInstance(
            tenant_id=tenant_id, client_id=client.id, client_name=client.name, name="Tracked",
            owner_id=member.id, status="paused",
            segments=[{
                "start": (exact_end - timedelta(minutes=5)).isoformat() + "Z",
                "end": exact_end.isoformat() + "Z",
            }],
            created_at=datetime.utcnow(),
        )
        recovery_task = models.TaskInstance(
            tenant_id=tenant_id, client_id=client.id, client_name=client.name, name="Recovery",
            owner_id=member.id, status="todo", segments=[], created_at=datetime.utcnow(),
        )
        db.add_all([tracked, recovery_task])
        db.commit()
        recovery_task_id = recovery_task.id

        recovery_end = browser_end + timedelta(minutes=10)
        db.info.pop("skip_tenant_scope", None)
        db.info["tenant_id"] = tenant_id
        updated = main.recover_task_time_batch(
            {
                "total_seconds": 600,
                "recovery_windows": [{
                    "start": browser_end.isoformat() + "Z",
                    "end": recovery_end.isoformat() + "Z",
                }],
                "allocations": [{"task_id": recovery_task_id, "seconds": 600}],
            },
            current_member=member,
            db=db,
        )

        recovered = [s for s in updated[0].segments if s.get("source") == "forgotten_time_recovery"]
        assert len(recovered) == 1
        assert recovered[0]["start"].startswith(browser_end.isoformat())
    finally:
        db.close()


def test_recovery_fractional_window_capacity_does_not_fail_whole_second_allocation():
    """A 600.x second active window should safely support a 600-second recovery allocation."""
    from datetime import timedelta

    tenant_id = "tenant_recovery_fractional_capacity"
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant = models.Tenant(id=tenant_id, name="Recovery fractional capacity", slug=tenant_id)
        db.add(tenant)
        user = models.User(email="recovery-fractional@example.com", password_hash="x", default_tenant_id=tenant_id)
        db.add(user)
        db.flush()
        member = models.Member(
            tenant_id=tenant_id, user_id=user.id, name="Recovery Fractional",
            email=user.email, role="member", timezone_name="Asia/Colombo",
        )
        db.add(member)
        db.flush()
        client = models.Client(tenant_id=tenant_id, name="Internal", code="FRAC")
        db.add(client)
        db.flush()
        task = models.TaskInstance(
            tenant_id=tenant_id, client_id=client.id, client_name=client.name,
            name="Fractional capacity recovery", owner_id=member.id,
            status="todo", segments=[], created_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()
        task_id = task.id

        window_end = datetime.utcnow() - timedelta(seconds=1)
        window_start = window_end - timedelta(seconds=600, milliseconds=600)

        db.info.pop("skip_tenant_scope", None)
        db.info["tenant_id"] = tenant_id
        updated = main.recover_task_time_batch(
            {
                "total_seconds": 600,
                "window_end_at": window_end.isoformat() + "Z",
                "recovery_windows": [
                    {"start": window_start.isoformat() + "Z", "end": window_end.isoformat() + "Z"},
                ],
                "allocations": [{"task_id": task_id, "seconds": 600}],
            },
            current_member=member,
            db=db,
        )

        segment = updated[0].segments[-1]
        assert segment["source"] == "forgotten_time_recovery"
        assert abs((main.parse_utc_naive(segment["end"]) - main.parse_utc_naive(segment["start"])).total_seconds() - 600) < 0.01
    finally:
        db.close()
