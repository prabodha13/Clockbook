"""Regression checks for optional birthdays and tenant-managed perspectives."""
import pytest
from fastapi import HTTPException
import database
import main
import models
import schemas
from perspective_content import PERSPECTIVES


def people(db, tenant_id, role="member"):
    db.info["skip_tenant_scope"] = True
    if not db.get(models.Tenant, tenant_id):
        db.add(models.Tenant(id=tenant_id, name=tenant_id, slug=tenant_id))
    user = models.User(email=f"{tenant_id}_{role}@example.com", password_hash="x")
    db.add(user)
    db.flush()
    member = models.Member(user_id=user.id, tenant_id=tenant_id, name=role, role=role, timezone_name="UTC")
    db.add(member)
    db.commit()
    db.info.pop("skip_tenant_scope", None)
    db.info["tenant_id"] = tenant_id
    return member


def test_birthday_validations_and_privacy():
    db = database.SessionLocal()
    try:
        member = people(db, "enhancement_birthday")
        saved = main.update_birthday(member.id, schemas.BirthdayPreferences(month=2, day=29, enabled=True), current_member=member, db=db)
        assert (saved["month"], saved["day"], saved["enabled"]) == (2, 29, True)
        with pytest.raises(HTTPException) as e:
            main.update_birthday(member.id, schemas.BirthdayPreferences(month=2, day=30), current_member=member, db=db)
        assert e.value.status_code == 400
        db.info.pop("tenant_id", None)
        other = people(db, "enhancement_birthday_other")
        with pytest.raises(HTTPException) as e:
            main.get_birthday(member.id, current_member=other, db=db)
        assert e.value.status_code == 404
    finally:
        db.close()


def test_tenant_perspectives_and_superadmin_only_management():
    assert len(PERSPECTIVES) >= 100 and len(PERSPECTIVES) == len(set(PERSPECTIVES))
    db = database.SessionLocal()
    try:
        staff = people(db, "enhancement_perspectives")
        listing = main.get_perspectives(current_member=staff, db=db)
        assert len(listing) == len(PERSPECTIVES)
        with pytest.raises(HTTPException) as e:
            main.create_perspective(schemas.PerspectiveSave(text="Test original perspective here"), current_member=staff, db=db)
        assert e.value.status_code == 403
        db.info.pop("tenant_id", None)
        admin = people(db, "enhancement_admin", role="super_admin")
        listing_other = main.get_perspectives(current_member=admin, db=db)
        assert len(listing_other) == len(PERSPECTIVES)
        added = main.create_perspective(schemas.PerspectiveSave(text="Question assumptions before choosing a direction"), current_member=admin, db=db)
        assert added["enabled"] is True
        assert len(main.manage_perspectives(current_member=admin, db=db)) == len(PERSPECTIVES) + 1
        main.edit_perspective(added["id"], schemas.PerspectiveSave(text=added["text"], enabled=False), current_member=admin, db=db)
        assert added["id"] not in {row["id"] for row in main.get_perspectives(current_member=admin, db=db)}
    finally:
        db.close()


def test_api_routes_precede_frontend_catchall():
    paths = [getattr(r, "path", "") for r in main.app.routes]
    if "/{full_path:path}" in paths:
        catchall = paths.index("/{full_path:path}")
        assert paths.index("/api/birthday/{member_id}") < catchall
        assert paths.index("/api/perspectives") < catchall
