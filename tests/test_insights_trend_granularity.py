from datetime import datetime, timedelta
from uuid import uuid4

import database
import main
import models


def _tenant(session):
    tenant_id = f"tenant_trend_{uuid4().hex[:10]}"
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    tenant = models.Tenant(id=tenant_id, name="Trend Test", slug=tenant_id)
    session.add(tenant)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return tenant


def _member(session, tenant_id):
    email = f"trend-{uuid4().hex[:8]}@example.com"
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(tenant_id=tenant_id, user_id=user.id, name="Trend User", email=email, role="super_admin")
    session.add(member)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _submitted_task(session, client, member, day, seconds=3600):
    end = datetime.combine(day, datetime.min.time()).replace(hour=10)
    start = end - timedelta(seconds=seconds)
    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name="Daily work",
        task_type="Review",
        owner_id=member.id,
        submitted_by_id=member.id,
        status="submitted",
        submitted_at=end,
        segments=[{"start": start.isoformat() + "Z", "end": end.isoformat() + "Z"}],
    )
    session.add(task)
    return task


def test_last_week_time_trend_is_daily_and_keeps_zero_days():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member = _member(s, tenant.id)
        client = models.Client(name="Trend Client", code=f"TR{uuid4().hex[:6]}")
        s.add(client)
        s.flush()

        today = datetime.utcnow().date()
        end = today - timedelta(days=1)
        start = end - timedelta(days=6)
        _submitted_task(s, client, member, start + timedelta(days=1), seconds=3600)
        _submitted_task(s, client, member, start + timedelta(days=4), seconds=7200)
        s.commit()

        result = main.get_insights(
            member_id=member.id,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            current_member=member,
            db=s,
        )

        trend = result["tracked_trend"]
        expected_days = [
            start + timedelta(days=i)
            for i in range(7)
            if (start + timedelta(days=i)).weekday() < 5
        ]
        assert [row["period_start"] for row in trend] == [day.isoformat() for day in expected_days]
        assert all(datetime.fromisoformat(row["period_start"]).weekday() < 5 for row in trend)

        by_day = {row["period_start"]: row for row in trend}
        first_task_day = start + timedelta(days=1)
        second_task_day = start + timedelta(days=4)
        if first_task_day.weekday() < 5:
            assert by_day[first_task_day.isoformat()]["seconds"] == 3600
        if second_task_day.weekday() < 5:
            assert by_day[second_task_day.isoformat()]["seconds"] == 7200
    finally:
        s.close()
