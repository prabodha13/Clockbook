from datetime import date, datetime, timedelta

import pytest
hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st

import main
import models


@given(
    start_offset=st.integers(min_value=0, max_value=86400 * 365),
    duration=st.integers(min_value=-86400, max_value=86400),
)
@settings(max_examples=150, deadline=None)
def test_property_submitted_duration_never_negative(start_offset, duration):
    base = datetime(2026, 1, 1) + timedelta(seconds=start_offset)
    end = base + timedelta(seconds=duration)
    segments = [{"start": base.isoformat() + "Z", "end": end.isoformat() + "Z"}]
    assert main.elapsed_seconds(segments) >= 0


@given(
    weekly_hours=st.floats(min_value=0, max_value=168, allow_nan=False, allow_infinity=False),
    unavailable=st.floats(min_value=0, max_value=7 * 24 * 3600, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, deadline=None)
def test_property_capacity_never_negative(weekly_hours, unavailable):
    member = models.Member(
        tenant_id="tenant_property",
        name="Property",
        role="member",
        weekly_capacity_hours=weekly_hours,
        capacity_effective_from=date(2026, 1, 5),
    )
    day = date(2026, 1, 5)
    value = main._insights_capacity_seconds(member, day, day, {day.isoformat(): unavailable})
    assert value >= 0


@given(st.lists(st.sampled_from(["member", "admin", "super_admin"]), min_size=1, max_size=20))
@settings(max_examples=50, deadline=None)
def test_property_role_values_stay_within_authorization_domain(roles):
    assert all(role in {"member", "admin", "super_admin"} for role in roles)
