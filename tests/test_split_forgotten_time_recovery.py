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
