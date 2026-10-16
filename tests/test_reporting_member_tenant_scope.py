"""The shared Help and reporting visibility helper must respect tenant and pod scope."""
import database
import main
import models
from test_security_integrity_regression_phase1 import _tenant, _member


def test_help_and_reporting_member_scope_is_tenant_and_pod_limited():
    db = database.SessionLocal()
    try:
        _tenant(db, "reports_scope_a", "reports-scope-a")
        db.info["tenant_id"] = "reports_scope_a"
        p1 = models.Pod(name="Reports pod one")
        p2 = models.Pod(name="Reports pod two")
        db.add_all([p1, p2]); db.commit()
        admin = _member(db, "reports_scope_a", "reports-scope-admin-a@example.com", "admin", p1.id)
        colleague = _member(db, "reports_scope_a", "reports-scope-colleague-a@example.com", "member", p1.id)
        other_pod = _member(db, "reports_scope_a", "reports-scope-other-pod@example.com", "member", p2.id)
        super_admin = _member(db, "reports_scope_a", "reports-scope-super-a@example.com", "super_admin")
        _tenant(db, "reports_scope_b", "reports-scope-b")
        outside = _member(db, "reports_scope_b", "reports-scope-other-tenant@example.com", "member")
        db.info["tenant_id"] = "reports_scope_a"
        assert main._insights_allowed_member_ids(admin, db) == {admin.id, colleague.id}
        assert other_pod.id not in main._insights_allowed_member_ids(admin, db)
        super_ids = main._insights_allowed_member_ids(super_admin, db)
        assert {admin.id, colleague.id, other_pod.id, super_admin.id}.issubset(super_ids)
        assert outside.id not in super_ids
    finally:
        db.close()
