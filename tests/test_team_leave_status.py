"""Team View leave status must be tenant/pod scoped and avoid revealing leave reasons."""
import pytest
from fastapi import HTTPException
import database
import main
import models
from test_dashboard_enhancements import people


def test_team_leave_only_visible_admin_pod(monkeypatch):
    db = database.SessionLocal()
    try:
        admin = people(db, "team_leave_scope", role="admin")
        teammate = people(db, "team_leave_scope", role="member")
        outside = people(db, "team_leave_scope", role="other")
        teammate.email = "teammate@example.com"
        outside.email = "outside@example.com"
        admin.pod_id = teammate.pod_id = "pod-a"
        outside.pod_id = "pod-b"
        db.commit()
        monkeypatch.setattr(main, "_calamari_credentials", lambda db: ("test", "key"))
        monkeypatch.setattr(main, "_calamari_post", lambda *a, **k: [
            {"employeeEmail": teammate.email, "status": "APPROVED", "absenceCategory": "TIMEOFF", "from": "2000-01-01", "to": "2100-01-01", "reason": "PRIVATE"},
            {"employeeEmail": outside.email, "status": "APPROVED", "absenceCategory": "TIMEOFF", "from": "2000-01-01", "to": "2100-01-01"},
        ])
        result = main.team_leave_today(current_member=admin, db=db)
        assert result["statuses"][teammate.id] == "on_leave"
        assert outside.id not in result["statuses"]
        assert "PRIVATE" not in str(result)
        with pytest.raises(HTTPException) as exc:
            main.team_leave_today(current_member=teammate, db=db)
        assert exc.value.status_code == 403
    finally:
        db.close()


def test_team_leave_disconnected_is_unknown(monkeypatch):
    db = database.SessionLocal()
    try:
        admin = people(db, "team_leave_disconnected", role="super_admin")
        monkeypatch.setattr(main, "_calamari_credentials", lambda db: None)
        result = main.team_leave_today(current_member=admin, db=db)
        assert result["connected"] is False
        assert result["statuses"][admin.id] == "unknown"
    finally:
        db.close()
