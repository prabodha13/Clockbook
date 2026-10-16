"""Workspace seasonal controls and delegated content-management permissions."""
import pytest
from fastapi import HTTPException
import database, main, models, schemas


def setup_member(db, tenant_id, role, permissions=None):
    db.info['skip_tenant_scope'] = True
    tenant = db.get(models.Tenant, tenant_id)
    if tenant is None:
        db.add(models.Tenant(id=tenant_id, name=tenant_id, slug=tenant_id))
    user = models.User(email=f'{tenant_id}_{role}_{len(permissions or [])}@test.local', password_hash='x')
    db.add(user)
    db.flush()
    member = models.Member(user_id=user.id, tenant_id=tenant_id, name=role, role=role, additional_permissions=permissions or [])
    db.add(member)
    db.commit()
    db.info.pop('skip_tenant_scope', None)
    db.info['tenant_id'] = tenant_id
    return member


def test_workspace_seasonal_settings_are_scoped_and_authorized():
    db = database.SessionLocal()
    try:
        admin = setup_member(db, 'seasonal_scope_a', 'admin')
        defaults = main.get_seasonal_settings(current_member=admin, db=db)
        assert all(defaults.values())
        with pytest.raises(HTTPException) as denied:
            main.update_seasonal_settings({**defaults, 'snow': False}, current_member=admin, db=db)
        assert denied.value.status_code == 403
        admin.additional_permissions = [main.PERMISSION_SEASONAL]
        db.commit()
        changed = main.update_seasonal_settings({**defaults, 'snow': False}, current_member=admin, db=db)
        assert changed['snow'] is False
        assert changed['halloween'] is True
        with pytest.raises(HTTPException) as invalid:
            main.update_seasonal_settings({'snow': 'false'}, current_member=admin, db=db)
        assert invalid.value.status_code == 422
        db.info.pop('tenant_id', None)
        other = setup_member(db, 'seasonal_scope_b', 'member')
        assert main.get_seasonal_settings(current_member=other, db=db)['snow'] is True
    finally:
        db.close()


def test_delegated_content_permissions_are_independent():
    admin = models.Member(role='admin', additional_permissions=[main.PERMISSION_PERSPECTIVES])
    main._require_delegated_admin_permission(admin, main.PERMISSION_PERSPECTIVES)
    with pytest.raises(HTTPException):
        main._require_delegated_admin_permission(admin, main.PERMISSION_BIRTHDAY_WISHES)
    with pytest.raises(HTTPException):
        main._require_delegated_admin_permission(admin, main.PERMISSION_SEASONAL)
    assert {main.PERMISSION_PERSPECTIVES, main.PERMISSION_BIRTHDAY_WISHES, main.PERMISSION_SEASONAL} <= main.DELEGATABLE_ADMIN_PERMISSIONS
