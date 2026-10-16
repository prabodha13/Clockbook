"""Quick Meeting availability must not disclose another tenant or pod's invitees."""
import pytest
from fastapi import HTTPException
import database
import main
import models
from test_security_integrity_regression_phase1 import _tenant, _member


def test_quick_meeting_availability_tenant_and_pod_scope(monkeypatch):
    db = database.SessionLocal()
    try:
        _tenant(db, 'quick_meeting_tenant_a', 'quick-meeting-a')
        p1 = models.Pod(name='Meetings Pod A')
        p2 = models.Pod(name='Meetings Pod B')
        db.add_all([p1, p2]); db.commit()
        admin = _member(db, 'quick_meeting_tenant_a', 'quick-meeting-admin-a@example.com', 'admin', p1.id)
        permitted = _member(db, 'quick_meeting_tenant_a', 'quick-meeting-user-a@example.com', 'member', p1.id)
        other_pod = _member(db, 'quick_meeting_tenant_a', 'quick-meeting-user-b@example.com', 'member', p2.id)
        _tenant(db, 'quick_meeting_tenant_b', 'quick-meeting-b')
        other_tenant = _member(db, 'quick_meeting_tenant_b', 'quick-meeting-user-c@example.com')
        db.info['tenant_id'] = 'quick_meeting_tenant_a'
        monkeypatch.setattr(main, 'get_google_access_token', lambda member, db: None)
        permitted_result = main.quick_meeting_availability({'attendee_member_ids': [permitted.id], 'duration_minutes': 30}, admin, db)
        assert permitted_result['members'][0]['status'] == 'unknown'
        for inaccessible in [other_pod, other_tenant]:
            with pytest.raises(HTTPException) as exc:
                main.quick_meeting_availability({'attendee_member_ids': [inaccessible.id], 'duration_minutes': 30}, admin, db)
            assert exc.value.status_code == 403
    finally:
        db.close()
