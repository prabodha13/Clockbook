from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

import database
import main
import models


def _tenant(session):
    tenant_id = f"tenant_client_work_{uuid4().hex[:10]}"
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    tenant = models.Tenant(id=tenant_id, name="Client Work Test", slug=tenant_id)
    session.add(tenant)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return tenant


def _member(session, tenant_id, role="member", name="User", pod_id=None):
    email = f"{uuid4().hex[:8]}@example.com"
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(tenant_id=tenant_id, user_id=user.id, name=name, email=email, role=role, pod_id=pod_id)
    session.add(member)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _submitted_task(session, client, member, *, name, task_type, seconds, field="Year-End Accounts", period_year=2025, period_type="year", submitted_pod_id=None, metric_label="", start_count=None, end_count=None, work_at=None):
    end = (work_at or datetime.utcnow()).replace(microsecond=0)
    start = end - timedelta(seconds=seconds)
    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name=name,
        task_type=task_type,
        source_template_field=field,
        owner_id=member.id,
        submitted_by_id=member.id,
        status="submitted",
        submitted_at=end,
        segments=[{"start": start.isoformat() + "Z", "end": end.isoformat() + "Z"}],
        period_type=period_type,
        period_year=period_year,
        submitted_pod_id=submitted_pod_id,
        tracks_number_label=metric_label,
        start_count=start_count,
        end_count=end_count,
    )
    session.add(task)
    return task


def test_client_work_is_billable_only_and_aggregates_existing_periods_and_metrics():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member = _member(s, tenant.id, name="Staff")
        client = models.Client(name="Client A", code=f"CW{uuid4().hex[:5]}")
        s.add(client)
        s.flush()
        s.add(models.TaskTypeOption(name="Billable Review", is_billable=True))
        s.add(models.TaskTypeOption(name="Internal Admin", is_billable=False))
        s.flush()

        _submitted_task(s, client, member, name="Accounts preparation", task_type="Billable Review", seconds=3600, metric_label="Transactions", start_count=0, end_count=100)
        _submitted_task(s, client, member, name="Accounts preparation", task_type="Billable Review", seconds=1800, metric_label="Transactions", start_count=100, end_count=160)
        _submitted_task(s, client, member, name="Internal admin", task_type="Internal Admin", seconds=7200, field="Internal")
        s.commit()

        result = main.get_insights_client_work(member_id=member.id, current_member=member, db=s)
        assert result["billable_seconds"] == 5400
        assert result["client_count"] == 1
        assert result["engagement_count"] == 1
        engagement = result["clients"][0]["engagements"][0]
        assert engagement["work_type"] == "Year-End Accounts"
        assert engagement["period"] == "2025"
        assert engagement["seconds"] == 5400
        assert len(engagement["tasks"]) == 1
        task_row = engagement["tasks"][0]
        assert task_row["task"] == "Accounts preparation"
        assert task_row["seconds"] == 5400.0
        assert task_row["records"] == 2
        assert len(task_row["entries"]) == 2
        assert sum(entry["seconds"] for entry in task_row["entries"]) == 5400.0
        assert engagement["metrics"] == [{"label": "Transactions", "quantity": 160}]
    finally:
        s.close()


def test_client_work_staff_cannot_view_another_member_but_super_admin_can():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        staff_a = _member(s, tenant.id, name="Staff A")
        staff_b = _member(s, tenant.id, name="Staff B")
        super_admin = _member(s, tenant.id, role="super_admin", name="Super Admin")
        client = models.Client(name="Client B", code=f"CB{uuid4().hex[:5]}")
        s.add(client)
        s.add(models.TaskTypeOption(name="Billable Tax", is_billable=True))
        s.flush()
        _submitted_task(s, client, staff_b, name="Tax return", task_type="Billable Tax", seconds=1200, field="Tax")
        s.commit()

        with pytest.raises(HTTPException) as forbidden:
            main.get_insights_client_work(member_id=staff_b.id, current_member=staff_a, db=s)
        assert forbidden.value.status_code == 403

        result = main.get_insights_client_work(member_id=staff_b.id, current_member=super_admin, db=s)
        assert result["member_id"] == staff_b.id
        assert result["billable_seconds"] == 1200
    finally:
        s.close()


def test_client_work_recent_filters_periods_but_keeps_full_history_inside_visible_period():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member = _member(s, tenant.id, name="Staff")
        client = models.Client(name="Bookkeeping Client", code=f"BK{uuid4().hex[:5]}")
        s.add(client)
        s.add(models.TaskTypeOption(name="Billable Bookkeeping", is_billable=True))
        s.flush()

        now = datetime.utcnow().replace(microsecond=0)
        # Same September bookkeeping period: one old entry and one recent entry.
        # Recent view must show the period and include BOTH durations in its total.
        first_same_period = _submitted_task(
            s, client, member, name="Bookkeeping", task_type="Billable Bookkeeping", seconds=3600,
            field="Bookkeeping & VAT", period_year=2026, period_type="month", work_at=now - timedelta(days=140),
        )
        recent_same_period = _submitted_task(
            s, client, member, name="Bookkeeping", task_type="Billable Bookkeeping", seconds=1800,
            field="Bookkeeping & VAT", period_year=2026, period_type="month", work_at=now - timedelta(days=5),
        )
        # Ensure both records share the exact same existing period key.
        recent_same_period.period_month = 9
        first_same_period.period_month = 9

        # Different old period with no recent activity should be hidden from Recent.
        old_period = _submitted_task(
            s, client, member, name="Bookkeeping", task_type="Billable Bookkeeping", seconds=7200,
            field="Bookkeeping & VAT", period_year=2025, period_type="month", work_at=now - timedelta(days=200),
        )
        old_period.period_month = 8
        s.commit()

        recent = main.get_insights_client_work(member_id=member.id, view="recent", current_member=member, db=s)
        assert recent["engagement_count"] == 1
        assert recent["billable_seconds"] == 5400
        engagement = recent["clients"][0]["engagements"][0]
        assert engagement["seconds"] == 5400
        assert engagement["task_records"] == 2

        all_periods = main.get_insights_client_work(member_id=member.id, view="all", current_member=member, db=s)
        assert all_periods["engagement_count"] == 2
        assert all_periods["billable_seconds"] == 12600
    finally:
        s.close()


def test_client_work_custom_window_selects_period_by_activity_but_keeps_full_period_total():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member = _member(s, tenant.id, name="Staff")
        client = models.Client(name="Custom Window Client", code=f"CW{uuid4().hex[:5]}")
        s.add(client)
        s.add(models.TaskTypeOption(name="Billable Bookkeeping", is_billable=True))
        s.flush()

        old = _submitted_task(
            s, client, member, name="Bookkeeping", task_type="Billable Bookkeeping", seconds=3600,
            field="Bookkeeping & VAT", period_year=2026, period_type="month", work_at=datetime(2026, 9, 10, 12, 0, 0),
        )
        recent = _submitted_task(
            s, client, member, name="Bookkeeping", task_type="Billable Bookkeeping", seconds=1800,
            field="Bookkeeping & VAT", period_year=2026, period_type="month", work_at=datetime(2026, 10, 3, 12, 0, 0),
        )
        old.period_month = 9
        recent.period_month = 9
        s.commit()

        result = main.get_insights_client_work(
            member_id=member.id,
            view="custom",
            date_from="2026-10-01",
            date_to="2026-10-09",
            current_member=member,
            db=s,
        )
        assert result["engagement_count"] == 1
        assert result["billable_seconds"] == 5400
        assert result["clients"][0]["engagements"][0]["seconds"] == 5400
        assert result["view_activity_from"] == "2026-10-01"
        assert result["view_activity_to"] == "2026-10-09"
    finally:
        s.close()


def test_client_work_week_to_date_and_month_to_date_show_full_history_for_qualifying_period():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member = _member(s, tenant.id, name="Staff")
        client = models.Client(name="Current Period Client", code=f"CP{uuid4().hex[:5]}")
        s.add(client)
        s.add(models.TaskTypeOption(name="Billable Bookkeeping", is_billable=True))
        s.flush()

        now = datetime.utcnow().replace(microsecond=0)
        older = _submitted_task(
            s, client, member, name="Bookkeeping", task_type="Billable Bookkeeping", seconds=3600,
            field="Bookkeeping & VAT", period_year=2026, period_type="month", work_at=now - timedelta(days=45),
        )
        current = _submitted_task(
            s, client, member, name="Bookkeeping", task_type="Billable Bookkeeping", seconds=1800,
            field="Bookkeeping & VAT", period_year=2026, period_type="month", work_at=now,
        )
        older.period_month = 9
        current.period_month = 9
        s.commit()

        for view in ("this_week", "this_month"):
            result = main.get_insights_client_work(member_id=member.id, view=view, current_member=member, db=s)
            assert result["engagement_count"] == 1
            assert result["billable_seconds"] == 5400
            task_row = result["clients"][0]["engagements"][0]["tasks"][0]
            assert task_row["records"] == 2
            assert len(task_row["entries"]) == 2
            assert sum(entry["seconds"] for entry in task_row["entries"]) == 5400.0
    finally:
        s.close()
