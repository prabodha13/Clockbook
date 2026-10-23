from datetime import datetime
import pytest
from fastapi import HTTPException
import database
import main
import models
import schemas
from test_additional_admin_permissions import _tenant, _member


def test_connection_diagnostics_requires_permission_and_respects_pods_and_tenants():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "diagnostics_tenant_a")
        pod_a = models.Pod(name="Diag A")
        pod_b = models.Pod(name="Diag B")
        s.add_all([pod_a, pod_b]); s.commit()
        root = _member(s, tenant.id, "diagroot@example.test", "super_admin")
        admin = _member(s, tenant.id, "diagadmin@example.test", "admin", pod_a.id)
        own = _member(s, tenant.id, "diagown@example.test", "member", pod_a.id)
        other = _member(s, tenant.id, "diagother@example.test", "member", pod_b.id)
        s.add_all([
            models.ConnectionDiagnosticEvent(member_id=own.id, event_type="permission_changed", details={"to": "denied"}),
            models.ConnectionDiagnosticEvent(member_id=other.id, event_type="connection_resumed", details={"gap_seconds": 300}),
        ]); s.commit()
        with pytest.raises(HTTPException) as denied:
            main.connection_diagnostics_report(current_member=admin, db=s)
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException) as denied_staff:
            main.connection_diagnostics_report(current_member=own, db=s)
        assert denied_staff.value.status_code == 403
        updated = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_CONNECTION_DIAGNOSTICS], expected_version=admin.version),
            current_member=root, db=s,
        )
        date = datetime.utcnow().strftime("%Y-%m-%d")
        rows = main.connection_diagnostics_report(date_from=date, date_to=date, current_member=updated, db=s)
        assert any(e["member_id"] == own.id for e in rows["events"])
        assert all(e["member_id"] != other.id for e in rows["events"])
        with pytest.raises(HTTPException) as out_scope:
            main.connection_diagnostics_report(member_id=other.id, current_member=updated, db=s)
        assert out_scope.value.status_code == 403
        # Cross-tenant rows may never appear, even with an identical date filter.
        tenant_b = _tenant(s, "diagnostics_tenant_b")
        outsider = _member(s, tenant_b.id, "diagoutsider@example.test", "member")
        s.add(models.ConnectionDiagnosticEvent(member_id=outsider.id, event_type="manual_logout", details={}))
        s.commit()
        s.info["tenant_id"] = tenant.id
        root_events = main.connection_diagnostics_report(date_from=date, date_to=date, current_member=root, db=s)
        assert all(e["member_id"] != outsider.id for e in root_events["events"])
    finally:
        s.close()
