from datetime import datetime

import database
import main
import models


def test_logout_pauses_running_timer_before_revoking_session():
    tenant_id = "tenant_logout_pause"
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant = db.get(models.Tenant, tenant_id)
        if tenant is None:
            tenant = models.Tenant(id=tenant_id, name="Logout pause", slug=tenant_id)
            db.add(tenant)
            db.commit()
        user = models.User(email="logout-pause@example.com", password_hash="x", default_tenant_id=tenant_id)
        db.add(user)
        db.flush()
        member = models.Member(
            tenant_id=tenant_id,
            user_id=user.id,
            name="Logout Pause",
            email=user.email,
            role="member",
        )
        db.add(member)
        db.flush()
        client = models.Client(tenant_id=tenant_id, name="Client", code="LOGOUT")
        db.add(client)
        db.flush()
        started = datetime.utcnow().replace(microsecond=0)
        task = models.TaskInstance(
            tenant_id=tenant_id,
            client_id=client.id,
            client_name=client.name,
            name="Running at logout",
            owner_id=member.id,
            status="running",
            segments=[{"start": started.isoformat() + "Z", "end": None}],
        )
        token = "logout-pause-token"
        session = models.Session(tenant_id=tenant_id, token=token, user_id=user.id, member_id=member.id)
        db.add_all([task, session])
        db.commit()
        task_id = task.id

        db.info.pop("skip_tenant_scope", None)
        db.info.pop("tenant_id", None)
        main.logout(authorization=f"Bearer {token}", db=db)

        db.info["tenant_id"] = tenant_id
        paused = db.get(models.TaskInstance, task_id)
        assert paused.status == "paused"
        assert paused.segments[-1]["end"] is not None
        end = datetime.fromisoformat(paused.segments[-1]["end"].replace("Z", "+00:00")).replace(tzinfo=None)
        assert end >= started

        db.info["skip_tenant_scope"] = True
        assert db.get(models.Session, token) is None
    finally:
        db.close()
