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


def _member(session, tenant_id, email, role="member", timezone_name=None, work_arrangement="office"):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(
        tenant_id=tenant_id, user_id=user.id, name=email, email=email, role=role,
        timezone_name=timezone_name, work_arrangement=work_arrangement,
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def test_initial_timezone_is_one_time_self_service():
    s = database.SessionLocal()
    try:
        tenant_id = "tenant_initial_timezone"
        _tenant(s, tenant_id)
        member = _member(s, tenant_id, "initial-zone@example.com")
        updated = main.set_initial_timezone(
            schemas.InitialTimezoneSet(timezone_name="Europe/Dublin"), current_member=member, db=s
        )
        assert updated.timezone_name == "Europe/Dublin"

        with pytest.raises(HTTPException) as exc:
            main.set_initial_timezone(
                schemas.InitialTimezoneSet(timezone_name="Asia/Colombo"), current_member=member, db=s
            )
        assert exc.value.status_code == 403
        assert member.timezone_name == "Europe/Dublin"
    finally:
        s.close()


def test_admin_can_change_work_arrangement_but_member_cannot():
    s = database.SessionLocal()
    try:
        tenant_id = "tenant_work_arrangement"
        _tenant(s, tenant_id)
        admin = _member(s, tenant_id, "arr-admin@example.com", role="super_admin", timezone_name="UTC")
        member = _member(s, tenant_id, "arr-member@example.com", timezone_name="UTC")
        updated = main.update_member_work_arrangement(
            member.id,
            schemas.MemberWorkArrangementUpdate(work_arrangement="remote", expected_version=member.version),
            current_member=admin, db=s,
        )
        assert updated.work_arrangement == "remote"

        with pytest.raises(HTTPException) as exc:
            main.update_member_work_arrangement(
                member.id,
                schemas.MemberWorkArrangementUpdate(work_arrangement="office", expected_version=updated.version),
                current_member=member, db=s,
            )
        assert exc.value.status_code == 403
    finally:
        s.close()
