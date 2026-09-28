from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import database
import main
import models
import schemas


def _tenant(session, tenant_id="tenant_delegation_exclusions"):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    tenant = session.get(models.Tenant, tenant_id)
    if tenant is None:
        tenant = models.Tenant(id=tenant_id, name="Delegation Settings", slug=tenant_id)
        session.add(tenant)
        session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return tenant


def _member(session, tenant_id, email, role):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(tenant_id=tenant_id, user_id=user.id, name=email, email=email, role=role)
    session.add(member)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _submitted_task(session, client, member, name, task_type, template):
    now = datetime.utcnow().replace(microsecond=0)
    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name=name,
        task_type=task_type,
        source_template_name=template,
        owner_id=member.id,
        submitted_by_id=member.id,
        status="submitted",
        submitted_at=now,
        segments=[{"start": (now - timedelta(minutes=10)).isoformat() + "Z", "end": now.isoformat() + "Z"}],
    )
    session.add(task)
    return task


def test_delegation_exclusions_are_super_admin_managed_and_filter_candidates():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        admin = _member(s, tenant.id, "delegation-super@example.com", "super_admin")
        staff = _member(s, tenant.id, "delegation-staff@example.com", "member")
        client = models.Client(name="Delegation Client", code="DGEX")
        s.add(client)
        s.flush()

        # Evidence exists because a staff member has completed the same signatures.
        _submitted_task(s, client, staff, "Monthly admin", "Admin", "Monthly Admin")
        _submitted_task(s, client, admin, "Monthly admin", "Admin", "Monthly Admin")
        _submitted_task(s, client, staff, "Monthly review", "Review", "Monthly Review")
        _submitted_task(s, client, admin, "Monthly review", "Review", "Monthly Review")
        s.commit()

        defaults = main.get_delegation_suggestion_exclusions(current_member=admin, db=s)
        assert "Admin" in defaults["exclusions"]
        assert main.BUILTIN_LEARNING_TASK_TYPE in defaults["exclusions"]

        today = datetime.utcnow().date().isoformat()
        result = main.get_insights(member_id=admin.id, date_from=today, date_to=today, current_member=admin, db=s)
        names = {row["task"] for row in result["delegation_candidates"]}
        assert "Monthly review" in names
        assert "Monthly admin" not in names

        main.update_delegation_suggestion_exclusions(
            schemas.DelegationSuggestionExclusionsUpdate(exclusions=["Support given", "Support received", main.BUILTIN_LEARNING_TASK_TYPE]),
            current_member=admin,
            db=s,
        )
        result = main.get_insights(member_id=admin.id, date_from=today, date_to=today, current_member=admin, db=s)
        names = {row["task"] for row in result["delegation_candidates"]}
        assert "Monthly admin" in names

        with pytest.raises(HTTPException) as forbidden:
            main.update_delegation_suggestion_exclusions(
                schemas.DelegationSuggestionExclusionsUpdate(exclusions=[]),
                current_member=staff,
                db=s,
            )
        assert forbidden.value.status_code == 403
    finally:
        s.close()
