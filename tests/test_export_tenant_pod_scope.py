"""Regression checks for Export to Karbon tenant, pod, and CSV access boundaries."""
import asyncio
from fastapi import HTTPException
import pytest
import database
import main
import models
from test_security_integrity_regression_phase1 import _tenant, _member, _client, _submitted_task


def test_export_table_csv_and_filters_share_pod_and_tenant_scope():
    db = database.SessionLocal()
    try:
        _tenant(db, "export_auth_a", "export-auth-a")
        p1 = models.Pod(name="Exports A")
        p2 = models.Pod(name="Exports B")
        db.add_all([p1, p2]); db.commit()
        admin = _member(db, "export_auth_a", "export-auth-admin-a@example.com", "admin", p1.id)
        member_a = _member(db, "export_auth_a", "export-auth-member-a@example.com", "member", p1.id)
        member_b = _member(db, "export_auth_a", "export-auth-member-b@example.com", "member", p2.id)
        client_a = _client(db, "Allowed export client", "EXP-A")
        task_a = _submitted_task(db, member_a, client_a, "Allowed export task", submitted_pod_id=p1.id)
        client_b = _client(db, "Other pod client", "EXP-B")
        task_b = _submitted_task(db, member_b, client_b, "Other pod task", submitted_pod_id=p2.id)
        _tenant(db, "export_auth_b", "export-auth-b")
        member_c = _member(db, "export_auth_b", "export-auth-member-c@example.com")
        client_c = _client(db, "Other tenant client", "EXP-C")
        task_c = _submitted_task(db, member_c, client_c, "Other tenant task")

        db.info["tenant_id"] = "export_auth_a"
        rows = main.get_export(pushed="all", current_member=admin, db=db)
        assert {row["id"] for row in rows} == {task_a.id}
        options = main.get_export_options(current_member=admin, db=db)
        assert {item["id"] for item in options["clients"]} == {client_a.id}
        csv_response = main.get_export_csv(pushed="all", current_member=admin, db=db)
        async def read_csv():
            return "".join([part.decode() if isinstance(part, bytes) else part async for part in csv_response.body_iterator])
        content = asyncio.run(read_csv())
        assert "Allowed export task" in content
        assert "Other pod task" not in content
        assert "Other tenant task" not in content
        for target in (member_b.id, member_c.id):
            with pytest.raises(HTTPException) as error:
                main.get_export(submitted_by=target, pushed="all", current_member=admin, db=db)
            assert error.value.status_code == 403
            with pytest.raises(HTTPException) as error:
                main.get_export_csv(submitted_by=target, pushed="all", current_member=admin, db=db)
            assert error.value.status_code == 403
        with pytest.raises(HTTPException) as error:
            main.get_export(pod_id=p2.id, pushed="all", current_member=admin, db=db)
        assert error.value.status_code == 403
    finally:
        db.close()
