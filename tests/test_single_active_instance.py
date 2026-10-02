from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import database
import main
import models


def _seed_user(db, suffix):
    tenant_id = f"tenant_single_instance_{suffix}"
    tenant = models.Tenant(id=tenant_id, name=f"Single instance {suffix}", slug=tenant_id)
    user = models.User(email=f"single-{suffix}@example.com", password_hash="x", default_tenant_id=tenant_id)
    db.add_all([tenant, user])
    db.flush()
    member = models.Member(
        tenant_id=tenant_id,
        user_id=user.id,
        name=f"Single {suffix}",
        email=user.email,
        role="member",
    )
    db.add(member)
    db.flush()
    return tenant, user, member


def test_active_instance_blocks_second_session_until_explicit_takeover():
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant, user, member = _seed_user(db, "active")
        first = models.Session(
            tenant_id=tenant.id,
            token="single-active-first",
            user_id=user.id,
            member_id=member.id,
            instance_id="browser-a",
            last_seen_at=datetime.utcnow(),
        )
        db.add(first)
        db.commit()

        with pytest.raises(HTTPException) as exc:
            main._create_single_user_session(db, user.id, member.id, tenant.id, "browser-b", takeover=False)
        assert exc.value.status_code == 409
        assert "already active" in str(exc.value.detail)

        new_token = main._create_single_user_session(db, user.id, member.id, tenant.id, "browser-b", takeover=True)
        db.commit()
        rows = db.query(models.Session).filter(models.Session.user_id == user.id).execution_options(skip_tenant_scope=True).all()
        assert len(rows) == 1
        assert rows[0].token == new_token
        assert rows[0].instance_id == "browser-b"
    finally:
        db.rollback()
        db.close()


def test_stale_instance_does_not_trap_user_out_of_clockbook():
    db = database.SessionLocal()
    try:
        db.info["skip_tenant_scope"] = True
        tenant, user, member = _seed_user(db, "stale")
        stale = models.Session(
            tenant_id=tenant.id,
            token="single-stale-old",
            user_id=user.id,
            member_id=member.id,
            instance_id="browser-old",
            last_seen_at=datetime.utcnow() - timedelta(seconds=main.ACTIVE_INSTANCE_WINDOW_SECONDS + 30),
        )
        db.add(stale)
        db.commit()

        new_token = main._create_single_user_session(db, user.id, member.id, tenant.id, "browser-new", takeover=False)
        db.commit()
        rows = db.query(models.Session).filter(models.Session.user_id == user.id).execution_options(skip_tenant_scope=True).all()
        assert len(rows) == 1
        assert rows[0].token == new_token
        assert rows[0].instance_id == "browser-new"
    finally:
        db.rollback()
        db.close()
