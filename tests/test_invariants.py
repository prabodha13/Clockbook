from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import random

import pytest
from fastapi import HTTPException

import database
import main
import models
import schemas


def seed(tenant_id, suffix, role="member"):
    s = database.SessionLocal(); s.info["skip_tenant_scope"] = True
    if not s.get(models.Tenant, tenant_id):
        s.add(models.Tenant(id=tenant_id, name=tenant_id, slug=tenant_id)); s.commit()
    user = models.User(email=f"{suffix}@example.com", password_hash="x", default_tenant_id=tenant_id)
    s.add(user); s.flush()
    member = models.Member(tenant_id=tenant_id, user_id=user.id, name=suffix, email=user.email, role=role)
    s.add(member); s.commit()
    s.info.pop("skip_tenant_scope", None); s.info["tenant_id"] = tenant_id
    client = models.Client(name="Client", code=f"C{suffix[:6].upper()}"); s.add(client); s.commit()
    return s, member, client


def make_task(s, member, client, name, status="todo", segments=None, **kwargs):
    kwargs.setdefault("role", "")
    kwargs.setdefault("task_type", "")
    t = models.TaskInstance(client_id=client.id, client_name=client.name, name=name, owner_id=member.id,
                            status=status, segments=list(segments or []), **kwargs)
    s.add(t); s.commit(); s.refresh(t); return t


def test_starting_different_tasks_leaves_exactly_one_running():
    s, member, client = seed("tenant_timer_switch", "timerswitch")
    try:
        main._ensure_one_running_timer_invariant(s)
        a = make_task(s, member, client, "A"); b = make_task(s, member, client, "B")
        main.start_task(a.id, schemas.TaskStart(), current_member=member, db=s)
        main.start_task(b.id, schemas.TaskStart(), current_member=member, db=s)
        running = s.query(models.TaskInstance).filter(models.TaskInstance.owner_id == member.id, models.TaskInstance.status == "running").all()
        assert [x.id for x in running] == [b.id]
        s.refresh(a); assert a.status == "paused"
    finally:
        s.close()


def test_concurrent_start_requests_cannot_leave_two_running():
    s, member, client = seed("tenant_timer_race", "timerrace")
    try:
        main._ensure_one_running_timer_invariant(s)
        a = make_task(s, member, client, "A"); b = make_task(s, member, client, "B")
        ids = [a.id, b.id]; member_id = member.id
    finally:
        s.close()

    def start(task_id):
        db = database.SessionLocal(); db.info["tenant_id"] = "tenant_timer_race"
        try:
            m = db.get(models.Member, member_id)
            try:
                main.start_task(task_id, schemas.TaskStart(), current_member=m, db=db)
                return "ok"
            except HTTPException as exc:
                assert exc.status_code == 409
                return "conflict"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(start, ids))
    check = database.SessionLocal(); check.info["tenant_id"] = "tenant_timer_race"
    try:
        assert check.query(models.TaskInstance).filter(models.TaskInstance.owner_id == member_id, models.TaskInstance.status == "running").count() == 1
    finally:
        check.close()


def test_pause_complete_and_reset_are_idempotent():
    s, member, client = seed("tenant_timer_idem", "timeridem")
    try:
        t = make_task(s, member, client, "Pause")
        main.start_task(t.id, schemas.TaskStart(), current_member=member, db=s)
        first = main.pause_task(t.id, schemas.TaskPause(), current_member=member, db=s)
        segs = list(first.segments)
        second = main.pause_task(t.id, schemas.TaskPause(), current_member=member, db=s)
        assert second.segments == segs

        r = make_task(s, member, client, "Reset")
        main.start_task(r.id, schemas.TaskStart(), current_member=member, db=s)
        main.reset_task(r.id, current_member=member, db=s)
        again = main.reset_task(r.id, current_member=member, db=s)
        assert again.status == "todo" and again.segments == []

        c = make_task(s, member, client, "Complete", status="paused", segments=[{"start":"2026-01-01T10:00:00Z","end":"2026-01-01T10:05:00Z"}])
        first_submit = main.submit_task(c.id, schemas.TaskSubmit(note="first"), current_member=member, db=s)
        submitted_at = first_submit.submitted_at
        second_submit = main.submit_task(c.id, schemas.TaskSubmit(note="second"), current_member=member, db=s)
        assert second_submit.submitted_at == submitted_at
        assert second_submit.note == "first"
    finally:
        s.close()


def test_elapsed_duration_never_negative_and_midnight_work_date_is_start_date():
    inverted = [{"start":"2026-01-02T00:05:00Z","end":"2026-01-02T00:01:00Z"}]
    assert main.elapsed_seconds(inverted) == 0
    s, member, client = seed("tenant_midnight", "midnight")
    try:
        t = make_task(s, member, client, "Midnight", status="submitted", segments=[{"start":"2026-01-01T23:59:00Z","end":"2026-01-02T00:01:00Z"}], submitted_at=datetime(2026,1,2,0,2))
        assert main.elapsed_seconds(t.segments) == 120
        assert main._insights_task_work_date(t).date() == date(2026,1,1)
    finally:
        s.close()


def test_property_elapsed_seconds_is_nonnegative_for_random_segments():
    rng = random.Random(42)
    base = datetime(2026, 1, 1)
    for _ in range(500):
        start = base + timedelta(seconds=rng.randint(0, 100000))
        end = start + timedelta(seconds=rng.randint(-5000, 5000))
        seg = [{"start": start.isoformat()+"Z", "end": end.isoformat()+"Z"}]
        assert main.elapsed_seconds(seg) >= 0


def test_export_keeps_distinct_business_dimensions_and_formula_cells_safe():
    s, member, client = seed("tenant_export_inv", "exportinv")
    try:
        t1 = make_task(s, member, client, "=Danger", status="submitted", segments=[{"start":"2026-02-01T10:00:00Z","end":"2026-02-01T10:10:00Z"}], submitted_at=datetime(2026,2,1,10,11), submitted_by_id=member.id, role="Role A", task_type="Type A", period_type="monthly", period_year=2026, period_number=1)
        t2 = make_task(s, member, client, "Other", status="submitted", segments=[{"start":"2026-02-01T11:00:00Z","end":"2026-02-01T11:10:00Z"}], submitted_at=datetime(2026,2,1,11,11), submitted_by_id=member.id, role="Role B", task_type="Type B", period_type="monthly", period_year=2026, period_number=2)
        rows = main.build_export_rows(s, "all", "all")
        ids = {r["id"] for r in rows}
        assert t1.id in ids and t2.id in ids and t1.id != t2.id
        assert main._csv_safe_text("=1+1") == "'=1+1"
        assert main._csv_safe_text("@evil") == "'@evil"
    finally:
        s.close()


def test_template_created_task_is_historical_snapshot_and_metric_required():
    s, member, client = seed("tenant_template_inv", "templateinv")
    try:
        s.add_all([models.Role(name="Bookkeeper"), models.TaskTypeOption(name="Review")]); s.commit()
        template = models.Template(field="Bookkeeping", name="Original Template"); s.add(template); s.flush()
        tt = models.TemplateTask(template_id=template.id, name="Review task", role="Bookkeeper", task_type="Review", tracks_number_label="Items")
        s.add(tt); s.commit()
        payload = schemas.TaskCreate(client_id=client.id, client_name="spoofed", name="Review task", source_template_task_id=tt.id)
        task = main.create_task(payload, current_member=member, db=s)
        assert task.client_name == client.name
        assert task.source_template_name == "Original Template"
        template.name = "Renamed Later"; tt.name = "Changed Later"; s.commit(); s.refresh(task)
        assert task.source_template_name == "Original Template" and task.name == "Review task"
        with pytest.raises(HTTPException) as exc:
            main.start_task(task.id, schemas.TaskStart(), current_member=member, db=s)
        assert exc.value.status_code == 400
    finally:
        s.close()


def test_capacity_properties_no_negative_no_double_reduce_half_day_and_effective_from():
    s, member, client = seed("tenant_capacity_inv", "capacityinv")
    try:
        member.weekly_capacity_hours = 40; member.capacity_effective_from = date(2026, 3, 2); s.commit()
        monday = date(2026,3,2)
        full_day = 8 * 3600
        assert main._insights_capacity_seconds(member, monday, monday, {monday.isoformat(): full_day * 2}) == 0
        assert main._insights_capacity_seconds(member, monday, monday, {monday.isoformat(): full_day / 2}) == full_day / 2
        assert main._insights_capacity_seconds(member, date(2026,2,23), date(2026,2,27)) == 0
    finally:
        s.close()


def test_optional_integrations_are_tenant_specific():
    s = database.SessionLocal(); s.info["skip_tenant_scope"] = True
    try:
        for tid in ["tenant_int_a", "tenant_int_b"]:
            if not s.get(models.Tenant, tid): s.add(models.Tenant(id=tid, name=tid, slug=tid))
        s.commit(); s.info.pop("skip_tenant_scope", None)
        s.info["tenant_id"] = "tenant_int_a"
        s.add_all([
            models.TenantSetting(key="karbon_application_id_encrypted", value="x"),
            models.TenantSetting(key="karbon_access_key_encrypted", value="y"),
            models.TenantSetting(key="karbon_config_mode", value="settings"),
            models.TenantSetting(key="calamari_tenant", value="tenant-a"),
            models.TenantSetting(key="calamari_api_key_encrypted", value="z"),
            models.TenantSetting(key="calamari_config_mode", value="settings"),
        ]); s.commit()
        assert main._karbon_connected_for_workspace(s) is True
        assert main._calamari_connected_for_workspace(s) is True
        s.info["tenant_id"] = "tenant_int_b"
        assert main._karbon_connected_for_workspace(s) is False
        assert main._calamari_connected_for_workspace(s) is False
    finally:
        s.close()


def test_invariant_diagnostics_detects_bad_segments_without_repairing():
    s, admin, client = seed("tenant_diag", "diag", role="super_admin")
    try:
        t = make_task(s, admin, client, "Bad", status="submitted", segments=[{"start":"2026-01-01T10:10:00Z","end":"2026-01-01T10:00:00Z"}])
        result = main.invariant_diagnostics(current_member=admin, db=s)
        assert result["ok"] is False
        assert any(f["type"] == "negative_segment" and f["task_id"] == t.id for f in result["findings"])
        s.refresh(t)
        assert t.segments[0]["end"] == "2026-01-01T10:00:00Z"
    finally:
        s.close()
