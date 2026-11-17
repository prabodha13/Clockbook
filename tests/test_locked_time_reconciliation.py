"""Regression coverage for the isolated Super Admin locked-time repair."""
from datetime import datetime

import pytest
from fastapi import HTTPException

import database
import main
import models
import schemas


def test_locked_time_repair_tenant_scope_permissions_idempotence():
    db = database.SessionLocal()
    try:
        db.info['skip_tenant_scope'] = True
        for tenant_id in ('locked_tenant_a', 'locked_tenant_b'):
            if db.get(models.Tenant, tenant_id) is None:
                db.add(models.Tenant(id=tenant_id, slug=tenant_id, name=tenant_id))
        db.commit()
        users = []
        for email, role, tenant_id in [
            ('lt-super@example.com', 'super_admin', 'locked_tenant_a'),
            ('lt-admin@example.com', 'admin', 'locked_tenant_a'),
            ('lt-staff@example.com', 'member', 'locked_tenant_a'),
            ('lt-other@example.com', 'member', 'locked_tenant_b'),
        ]:
            user = models.User(email=email, password_hash='x', default_tenant_id=tenant_id)
            db.add(user)
            db.flush()
            member = models.Member(tenant_id=tenant_id, user_id=user.id, name=email, email=email,
                                   role=role, timezone_name='UTC', additional_permissions=[])
            db.add(member)
            db.flush()
            users.append(member)
        db.commit()
        super_admin, admin, staff, outsider = users
        db.info.pop('skip_tenant_scope', None)
        db.info['tenant_id'] = 'locked_tenant_a'
        db.info['actor_member_id'] = super_admin.id
        client = models.Client(name='Repair Client', code='RC')
        db.add(client)
        db.flush()
        day = '2026-11-17'
        task = models.TaskInstance(client_id=client.id, client_name=client.name,
                                   name='Overlap', owner_id=staff.id, status='paused',
                                   segments=[{'start': '2026-11-17T12:00:00Z', 'end': '2026-11-17T14:00:00Z'}])
        db.add(task)
        db.add_all([
            models.InactivityEvent(member_id=staff.id, kind='screen_locked',
                                   started_at=datetime(2026,11,17,12,30), ended_at=datetime(2026,11,17,13), seconds=1800),
            models.InactivityEvent(member_id=staff.id, kind='screen_locked',
                                   started_at=datetime(2026,11,17,12,50), ended_at=datetime(2026,11,17,13,10), seconds=1200),
        ])
        db.commit()
        with pytest.raises(HTTPException) as forbidden:
            main.preview_locked_time_reconciliation(staff.id, day, current_member=admin, db=db)
        assert forbidden.value.status_code == 403
        with pytest.raises(HTTPException) as outside:
            main.preview_locked_time_reconciliation(outsider.id, day, current_member=super_admin, db=db)
        assert outside.value.status_code == 404
        preview = main.preview_locked_time_reconciliation(staff.id, day, current_member=super_admin, db=db)
        assert preview['total_deducted_seconds'] == 40 * 60
        with pytest.raises(HTTPException) as stale:
            main.apply_locked_time_reconciliation(schemas.LockedTimeReconciliationApply(
                member_id=staff.id, work_date=day, preview_token='old', reason='Screen lock overlap', selected_task_ids=[task.id]),
                current_member=super_admin, db=db)
        assert stale.value.status_code == 409
        result = main.apply_locked_time_reconciliation(schemas.LockedTimeReconciliationApply(
            member_id=staff.id, work_date=day, preview_token=preview['token'], reason='Screen lock overlap', selected_task_ids=[task.id]),
            current_member=super_admin, db=db)
        assert result['deducted_seconds'] == 2400
        db.refresh(task)
        assert len(task.segments) == 2
        assert task.segments[0]['end'] == '2026-11-17T12:30:00Z'
        assert task.segments[1]['start'] == '2026-11-17T13:10:00Z'
        fresh = main.preview_locked_time_reconciliation(staff.id, day, current_member=super_admin, db=db)
        assert fresh['total_deducted_seconds'] == 0
        assert len(fresh['history']) == 1
        assert len(db.query(models.LockedTimeCorrection).all()) == 1
        with pytest.raises(HTTPException):
            main.apply_locked_time_reconciliation(schemas.LockedTimeReconciliationApply(
                member_id=staff.id, work_date=day, preview_token=preview['token'], reason='Duplicate adjustment', selected_task_ids=[task.id]),
                current_member=super_admin, db=db)
    finally:
        db.close()


def test_locked_time_repair_only_selected_tasks():
    db = database.SessionLocal()
    try:
        db.info['skip_tenant_scope'] = True
        tenant = models.Tenant(id='selection_tenant', slug='selection_tenant', name='selection_tenant')
        db.add(tenant)
        user = models.User(email='lt-selection-super@example.com', password_hash='x', default_tenant_id=tenant.id)
        db.add(user)
        db.flush()
        actor = models.Member(tenant_id=tenant.id, user_id=user.id, name='Selection super', email=user.email,
                              role='super_admin', timezone_name='UTC', additional_permissions=[])
        db.add(actor)
        db.flush()
        staff_user = models.User(email='lt-selection-staff@example.com', password_hash='x', default_tenant_id=tenant.id)
        db.add(staff_user)
        db.flush()
        staff = models.Member(tenant_id=tenant.id, user_id=staff_user.id, name='Selection staff', email=staff_user.email,
                              role='member', timezone_name='UTC', additional_permissions=[])
        db.add(staff)
        db.commit()
        db.info.pop('skip_tenant_scope', None)
        db.info['tenant_id'] = tenant.id
        db.info['actor_member_id'] = actor.id
        client = models.Client(name='Selection Client', code='SC')
        db.add(client)
        db.flush()
        first = models.TaskInstance(client_id=client.id, client_name=client.name, name='First', owner_id=staff.id,
                                    status='submitted', segments=[{'start': '2026-11-17T10:00:00Z', 'end': '2026-11-17T11:00:00Z'}])
        second = models.TaskInstance(client_id=client.id, client_name=client.name, name='Second', owner_id=staff.id,
                                     status='submitted', segments=[{'start': '2026-11-17T12:00:00Z', 'end': '2026-11-17T13:00:00Z'}])
        db.add_all([first, second, models.InactivityEvent(member_id=staff.id, kind='screen_locked',
                    started_at=datetime(2026,11,17,10,15), ended_at=datetime(2026,11,17,10,30), seconds=900),
                    models.InactivityEvent(member_id=staff.id, kind='screen_locked',
                    started_at=datetime(2026,11,17,12,15), ended_at=datetime(2026,11,17,12,30), seconds=900)])
        db.commit()
        before_second = list(second.segments)
        preview = main.preview_locked_time_reconciliation(staff.id, '2026-11-17', current_member=actor, db=db)
        assert len(preview['changes']) == 2
        def apply(selected):
            return main.apply_locked_time_reconciliation(schemas.LockedTimeReconciliationApply(
                member_id=staff.id, work_date='2026-11-17', preview_token=preview['token'],
                reason='Approved selected locked time', selected_task_ids=selected), current_member=actor, db=db)
        with pytest.raises(HTTPException) as empty:
            apply([])
        assert empty.value.status_code == 400
        with pytest.raises(HTTPException) as invalid:
            apply(['another_task'])
        assert invalid.value.status_code == 409
        result = apply([first.id])
        assert result['updated_tasks'] == 1
        assert result['deducted_seconds'] == 900
        db.refresh(first)
        db.refresh(second)
        assert len(first.segments) == 2
        assert second.segments == before_second
        fresh = main.preview_locked_time_reconciliation(staff.id, '2026-11-17', current_member=actor, db=db)
        assert [c['task_id'] for c in fresh['changes']] == [second.id]
    finally:
        db.close()
