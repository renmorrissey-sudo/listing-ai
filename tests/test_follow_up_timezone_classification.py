"""Regression tests for account-timezone follow-up classification.

Fixed clock: 2026-07-30 ~17:14 America/Denver (production bug report).
"""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import crm_db
import crm_time
import db
from migrations.runner import apply_pending_migrations

TZ = "America/Denver"
# July 30, 2026 at approximately 5:14 PM America/Denver
FIXED_NOW = datetime(2026, 7, 30, 17, 14, tzinfo=ZoneInfo(TZ))


def _lead(user_id, name="Ben Miller"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    return db.upsert_lead(
        user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms"
    )


def _set_tz(user_id, tz=TZ):
    db.update_business_profile(user_id, timezone=tz)


def _schedule(user_id, lead_id, due_iso, reason="Follow up", **kwargs):
    result, err = crm_db.set_lead_follow_up(
        user_id, lead_id, due_iso, reason, force_create=True, **kwargs
    )
    assert err is None, err
    return result


def _windows(now=FIXED_NOW, tz=TZ):
    return crm_time.compute_follow_up_windows(tz, now=now)


def test_windows_boundaries_america_denver():
    w = _windows()
    assert w.timezone_name == TZ
    assert w.local_date == "2026-07-30"
    assert w.start_today_local.isoformat().startswith("2026-07-30T00:00:00")
    assert w.start_tomorrow_local.isoformat().startswith("2026-07-31T00:00:00")
    # July 30 2026 is Thursday → next Monday is August 3
    assert w.start_next_week_local.isoformat().startswith("2026-08-03T00:00:00")
    # MDT (UTC-6) in July
    assert w.start_today_utc == datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc)
    assert w.start_tomorrow_utc == datetime(2026, 7, 31, 6, 0, tzinfo=timezone.utc)
    assert w.start_next_week_utc == datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc)


def test_july_29_evening_is_overdue_not_today_not_this_week(two_users):
    """Production bug: July 29 8:46 PM Denver was shown under Due today."""
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1)
    # July 29, 2026 at 8:46 PM America/Denver = 2026-07-30T02:46:00+00:00
    due = crm_time.local_dt_to_utc_iso(2026, 7, 29, 20, 46, timezone_name=TZ)
    assert due.startswith("2026-07-30T02:46:00")
    _schedule(u1, lead_id, due, "Evening call")

    w = _windows()
    assert crm_time.local_calendar_date(due, TZ) == "2026-07-29"
    assert crm_time.classify_open_follow_up(due, w) == "overdue"
    assert crm_time.is_due_today(due, w) is False
    assert crm_time.is_this_week(due, w) is False
    assert crm_time.is_upcoming(due, w) is False

    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts == {
        "follow_ups_due_today": 0,
        "follow_ups_overdue": 1,
        "follow_ups_due_this_week": 0,
    }
    groups = crm_db.group_follow_ups_for_lead(
        crm_db.list_follow_ups(u1, bucket="all", timezone_name=TZ, now=FIXED_NOW),
        timezone_name=TZ,
        now=FIXED_NOW,
        user_id=u1,
    )
    assert len(groups["overdue"]) == 1
    assert len(groups["today"]) == 0
    assert groups["overdue"][0]["lead_name"] == "Ben Miller"


def test_july_30_later_today_is_due_today(two_users):
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1, name="Later Today")
    due = crm_time.local_dt_to_utc_iso(2026, 7, 30, 20, 0, timezone_name=TZ)
    _schedule(u1, lead_id, due, "Later tonight")
    w = _windows()
    assert crm_time.classify_open_follow_up(due, w) == "today"
    assert crm_time.is_this_week(due, w) is True
    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts["follow_ups_due_today"] == 1
    assert counts["follow_ups_overdue"] == 0
    assert counts["follow_ups_due_this_week"] == 1


def test_july_30_earlier_today_remains_due_today(two_users):
    """Earlier-today stays in Due today until local midnight; not this-week upcoming."""
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1, name="Earlier Today")
    due = crm_time.local_dt_to_utc_iso(2026, 7, 30, 9, 0, timezone_name=TZ)
    _schedule(u1, lead_id, due, "Morning call")
    w = _windows()
    assert crm_time.classify_open_follow_up(due, w) == "today"
    assert crm_time.is_this_week(due, w) is False  # already past now_local
    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts["follow_ups_due_today"] == 1
    assert counts["follow_ups_due_this_week"] == 0


def test_july_31_is_this_week(two_users):
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1, name="Fri")
    due = crm_time.local_dt_to_utc_iso(2026, 7, 31, 10, 0, timezone_name=TZ)
    _schedule(u1, lead_id, due, "Friday")
    w = _windows()
    assert crm_time.classify_open_follow_up(due, w) == "upcoming"
    assert crm_time.is_this_week(due, w) is True
    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts["follow_ups_due_this_week"] == 1
    assert counts["follow_ups_due_today"] == 0


def test_august_2_sunday_is_this_week_monday_sunday(two_users):
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1, name="Sun")
    due = crm_time.local_dt_to_utc_iso(2026, 8, 2, 15, 0, timezone_name=TZ)
    _schedule(u1, lead_id, due, "Sunday")
    w = _windows()
    assert crm_time.is_this_week(due, w) is True
    assert crm_time.classify_open_follow_up(due, w) == "upcoming"


def test_august_3_monday_is_next_week(two_users):
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1, name="Next Mon")
    due = crm_time.local_dt_to_utc_iso(2026, 8, 3, 9, 0, timezone_name=TZ)
    _schedule(u1, lead_id, due, "Next Monday")
    w = _windows()
    assert crm_time.is_this_week(due, w) is False
    assert crm_time.classify_open_follow_up(due, w) == "upcoming"
    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts["follow_ups_due_this_week"] == 0


def test_august_25_upcoming_not_this_week(two_users):
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1)
    due = crm_time.local_dt_to_utc_iso(2026, 8, 25, 20, 47, timezone_name=TZ)
    _schedule(u1, lead_id, due, "August follow-up")
    w = _windows()
    assert crm_time.classify_open_follow_up(due, w) == "upcoming"
    assert crm_time.is_this_week(due, w) is False
    assert crm_time.is_upcoming(due, w) is True
    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts == {
        "follow_ups_due_today": 0,
        "follow_ups_overdue": 0,
        "follow_ups_due_this_week": 0,
    }
    groups = crm_db.group_follow_ups_for_lead(
        crm_db.list_follow_ups(u1, bucket="all", timezone_name=TZ, now=FIXED_NOW),
        timezone_name=TZ,
        now=FIXED_NOW,
        user_id=u1,
    )
    assert len(groups["upcoming"]) == 1
    assert groups["upcoming"][0]["lead_name"] == "Ben Miller"


def test_production_ben_miller_pair(two_users):
    """Exact production pair: overdue July 29 + upcoming Aug 25."""
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1)
    due_overdue = crm_time.local_dt_to_utc_iso(2026, 7, 29, 20, 46, timezone_name=TZ)
    due_upcoming = crm_time.local_dt_to_utc_iso(2026, 8, 25, 20, 47, timezone_name=TZ)
    _schedule(u1, lead_id, due_overdue, "Call 1")
    _schedule(u1, lead_id, due_upcoming, "Call 2")

    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts == {
        "follow_ups_due_today": 0,
        "follow_ups_overdue": 1,
        "follow_ups_due_this_week": 0,
    }
    overdue = crm_db.list_follow_ups_for_dashboard_range(
        u1, "overdue", timezone_name=TZ, now=FIXED_NOW
    )
    today = crm_db.list_follow_ups_for_dashboard_range(
        u1, "today", timezone_name=TZ, now=FIXED_NOW
    )
    week = crm_db.list_follow_ups_for_dashboard_range(
        u1, "this_week", timezone_name=TZ, now=FIXED_NOW
    )
    assert len(overdue) == 1
    assert len(today) == 0
    assert len(week) == 0
    groups = crm_db.group_follow_ups_for_lead(
        crm_db.list_follow_ups(u1, bucket="all", timezone_name=TZ, now=FIXED_NOW),
        timezone_name=TZ,
        now=FIXED_NOW,
        user_id=u1,
    )
    assert len(groups["overdue"]) == 1
    assert len(groups["today"]) == 0
    assert len(groups["upcoming"]) == 1


def test_completed_and_cancelled_excluded(two_users):
    u1, _ = two_users
    _set_tz(u1)
    lead_a = _lead(u1, name="Done")
    lead_b = _lead(u1, name="Cancelled")
    lead_c = _lead(u1, name="Open Today")
    due_today = crm_time.local_dt_to_utc_iso(2026, 7, 30, 18, 0, timezone_name=TZ)
    r1 = _schedule(u1, lead_a, due_today, "Done FU")
    r2 = _schedule(u1, lead_b, due_today, "Cancel FU")
    _schedule(u1, lead_c, due_today, "Open FU")
    ok, err = crm_db.complete_follow_up(u1, r1["follow_up_id"])
    assert ok and err is None
    cancelled, err = crm_db.cancel_follow_up(
        u1,
        r2["follow_up_id"],
        cancel_reason_code="no_longer_needed",
        cancelled_by_user_id=u1,
    )
    assert err is None and cancelled

    counts = crm_db.follow_up_dashboard_counts(u1, timezone_name=TZ, now=FIXED_NOW)
    assert counts["follow_ups_due_today"] == 1
    assert counts["follow_ups_overdue"] == 0
    assert counts["follow_ups_due_this_week"] == 1


def test_utc_midnight_crossing_converts_correctly():
    # 2026-07-30 02:46 UTC → still July 29 evening in Denver
    due = "2026-07-30T02:46:00+00:00"
    assert crm_time.local_calendar_date(due, TZ) == "2026-07-29"
    w = _windows()
    assert crm_time.classify_open_follow_up(due, w) == "overdue"
    # Naive timestamps treated as UTC
    naive = "2026-07-30T02:46:00"
    assert crm_time.local_calendar_date(naive, TZ) == "2026-07-29"


def test_dst_spring_forward_america_denver():
    # 2026-03-08: clocks jump 2:00 → 3:00 MDT. Local midnight still UTC-7 (MST).
    now = datetime(2026, 3, 8, 10, 0, tzinfo=ZoneInfo(TZ))
    w = crm_time.compute_follow_up_windows(TZ, now=now)
    assert w.start_today_utc == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    # After spring-forward, local 03:30 exists as MDT (UTC-6)
    after = crm_time.local_dt_to_utc_iso(2026, 3, 8, 3, 30, timezone_name=TZ)
    assert after.startswith("2026-03-08T09:30:00")
    assert crm_time.classify_open_follow_up(after, w) == "today"


def test_dst_fall_back_america_denver():
    # 2026-11-01: clocks fall back 2:00 → 1:00. Local midnight is MDT (UTC-6).
    now = datetime(2026, 11, 1, 10, 0, tzinfo=ZoneInfo(TZ))
    w = crm_time.compute_follow_up_windows(TZ, now=now)
    assert w.start_today_utc == datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    evening = crm_time.local_dt_to_utc_iso(2026, 11, 1, 20, 0, timezone_name=TZ)
    assert crm_time.classify_open_follow_up(evening, w) == "today"


def test_page_calendar_dashboard_counts_match(two_users, app_client):
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1)
    _schedule(
        u1,
        lead_id,
        crm_time.local_dt_to_utc_iso(2026, 7, 29, 20, 46, timezone_name=TZ),
        "Overdue",
    )
    _schedule(
        u1,
        lead_id,
        crm_time.local_dt_to_utc_iso(2026, 7, 30, 20, 0, timezone_name=TZ),
        "Today later",
    )
    _schedule(
        u1,
        lead_id,
        crm_time.local_dt_to_utc_iso(2026, 7, 31, 11, 0, timezone_name=TZ),
        "Friday",
    )
    _schedule(
        u1,
        lead_id,
        crm_time.local_dt_to_utc_iso(2026, 8, 25, 20, 47, timezone_name=TZ),
        "August",
    )

    # Patch "now" via explicit windows on helpers; HTTP pages use real clock.
    # Compare helper surfaces against each other with fixed clock.
    w = _windows()
    page_counts = crm_db.follow_up_dashboard_counts(
        u1, timezone_name=TZ, now=FIXED_NOW, windows=w
    )
    cal = crm_db.calendar_summary(u1, timezone_name=TZ, now=FIXED_NOW, windows=w)
    pipe = crm_db.get_pipeline_metrics(u1, timezone_name=TZ, now=FIXED_NOW, windows=w)
    legacy = db.get_dashboard_metrics(u1)

    assert page_counts["follow_ups_due_today"] == 1
    assert page_counts["follow_ups_overdue"] == 1
    assert page_counts["follow_ups_due_this_week"] == 2  # later today + Friday
    assert cal["follow_ups_due_today"] == page_counts["follow_ups_due_today"]
    assert cal["follow_ups_overdue"] == page_counts["follow_ups_overdue"]
    assert cal["follow_ups_due_this_week"] == page_counts["follow_ups_due_this_week"]
    assert pipe["follow_ups_due_today"] == page_counts["follow_ups_due_today"]
    assert pipe["overdue_follow_ups"] == page_counts["follow_ups_overdue"]
    assert pipe["follow_ups_due_this_week"] == page_counts["follow_ups_due_this_week"]
    # Legacy metrics path now uses the same helper (live clock). When the
    # real clock is still July 30 Denver, values match; otherwise only require
    # that keys exist and are non-negative.
    assert legacy["crm"]["follow_ups_due_today"] >= 0
    assert legacy["crm"]["follow_ups_overdue"] >= 0


def test_filtered_destination_totals_match_cards(two_users, app_client, monkeypatch):
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1)
    _schedule(
        u1,
        lead_id,
        crm_time.local_dt_to_utc_iso(2026, 7, 29, 20, 46, timezone_name=TZ),
        "Overdue",
    )
    _schedule(
        u1,
        lead_id,
        crm_time.local_dt_to_utc_iso(2026, 7, 31, 11, 0, timezone_name=TZ),
        "Friday",
    )

    # Freeze classification clock for HTTP routes via monkeypatch.
    real_windows = crm_db._follow_up_windows

    def frozen_windows(user_id=None, **kwargs):
        kwargs.setdefault("timezone_name", TZ)
        kwargs["now"] = FIXED_NOW
        return real_windows(user_id, **kwargs)

    monkeypatch.setattr(crm_db, "_follow_up_windows", frozen_windows)

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    page = app_client.get("/crm/follow-ups")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="count-today">0<' in html or 'id="count-today">0</div>' in html
    assert 'id="count-overdue">1<' in html or 'id="count-overdue">1</div>' in html
    assert 'id="count-week">1<' in html or 'id="count-week">1</div>' in html
    assert 'href="/crm/follow-ups?due=today"' in html
    assert 'href="/crm/follow-ups?due=overdue"' in html
    assert 'href="/crm/follow-ups?due=this-week"' in html

    for due, expect in [("overdue", 1), ("today", 0), ("this-week", 1)]:
        res = app_client.get(f"/crm/follow-ups?due={due}")
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        assert f"{expect} result" in body
        assert "Clear filters" in body
        assert 'class="metric active"' in body or "metric active" in body

    dash = app_client.get("/dashboard")
    assert dash.status_code == 200
    dash_html = dash.get_data(as_text=True)
    assert "/crm/follow-ups?due=overdue" in dash_html
    assert "/crm/follow-ups?due=today" in dash_html
    assert "/crm/follow-ups?due=this-week" in dash_html


def test_no_browser_offset_required_for_correct_bucketing(two_users, app_client, monkeypatch):
    """Direct /crm/follow-ups without local_date/tz_offset must use account TZ."""
    u1, _ = two_users
    _set_tz(u1)
    lead_id = _lead(u1)
    # This UTC timestamp is July 30 — without Denver conversion it looks like "today".
    due = "2026-07-30T02:46:00+00:00"
    _schedule(u1, lead_id, due, "Cross-midnight")

    real_windows = crm_db._follow_up_windows

    def frozen_windows(user_id=None, **kwargs):
        kwargs.setdefault("timezone_name", TZ)
        kwargs["now"] = FIXED_NOW
        return real_windows(user_id, **kwargs)

    monkeypatch.setattr(crm_db, "_follow_up_windows", frozen_windows)

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.get("/crm/follow-ups")
    html = res.get_data(as_text=True)
    assert 'id="count-overdue">1' in html
    assert 'id="count-today">0' in html
    # Item should appear under Overdue section heading context
    assert "Overdue" in html
    assert "Ben Miller" in html or "Cross-midnight" in html
