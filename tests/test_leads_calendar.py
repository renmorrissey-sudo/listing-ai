"""Unified Leads Calendar events from follow-ups, tasks, and appointments."""

import uuid
from datetime import datetime, timedelta, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations


def _lead(user_id, name="Cal Lead"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    return db.upsert_lead(
        user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms"
    )


def test_follow_ups_tasks_appointments_appear_on_calendar(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "Unified Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    crm_db.set_lead_follow_up(u1, lead_id, due, "Buyer consultation")
    task_id, err = crm_db.create_task(
        u1, {"title": "Send CMA", "lead_id": lead_id, "due_at": due, "task_type": "prepare_cma"}
    )
    assert err is None
    start = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    appt_id, err = crm_db.create_appointment(
        u1,
        {
            "lead_id": lead_id,
            "appointment_type": "property_showing",
            "start_at": start,
            "end_at": start,
        },
    )
    assert err is None

    events = crm_db.list_calendar_events(u1)
    ids = {e["id"] for e in events}
    assert any(i.startswith("followup:") for i in ids)
    assert f"task:{task_id}" in ids
    assert f"appointment:{appt_id}" in ids
    showing = [e for e in events if e["id"] == f"appointment:{appt_id}"][0]
    assert showing["event_type"] == "showing"
    assert showing["lead_name"] == "Unified Lead"


def test_cancelling_removes_from_default_active_views(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result, _ = crm_db.set_lead_follow_up(u1, lead_id, due, "Follow up")
    fid = result["follow_up_id"]
    active = crm_db.list_calendar_events(u1, include_cancelled=False)
    assert any(e["id"] == f"followup:{fid}" for e in active)
    crm_db.cancel_follow_up(u1, fid, cancel_reason_code="no_longer_needed")
    active2 = crm_db.list_calendar_events(u1, include_cancelled=False)
    assert not any(e["id"] == f"followup:{fid}" for e in active2)
    with_cancelled = crm_db.list_calendar_events(u1, include_cancelled=True)
    assert any(e["id"] == f"followup:{fid}" for e in with_cancelled)


def test_rescheduling_changes_calendar_date(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due1 = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    due2 = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    result, _ = crm_db.set_lead_follow_up(u1, lead_id, due1, "Follow up")
    fid = result["follow_up_id"]
    crm_db.update_follow_up(u1, fid, due_at=due2)
    events = [e for e in crm_db.list_calendar_events(u1) if e["id"] == f"followup:{fid}"]
    assert len(events) == 1
    assert events[0]["start_at"] == due2


def test_event_filters_and_stable_ids(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    crm_db.set_lead_follow_up(u1, lead_id, due, "SMS check-in")
    crm_db.create_task(
        u1,
        {
            "title": "Text the lead",
            "lead_id": lead_id,
            "due_at": due,
            "task_type": "send_sms",
            "priority": "high",
        },
    )
    sms_events = crm_db.list_calendar_events(u1, event_types=["sms_follow_up"])
    assert sms_events
    assert all(e["event_type"] == "sms_follow_up" for e in sms_events)
    high = crm_db.list_calendar_events(u1, priorities=["high"])
    assert all(e["priority"] == "high" for e in high)
    # Stable unique ids even if listed twice conceptually.
    events = crm_db.list_calendar_events(u1)
    ids = [e["id"] for e in events]
    assert len(ids) == len(set(ids))


def test_lead_less_task_does_not_masquerade_as_a_follow_up(two_users):
    """A generic task with no lead attached (e.g. "Create a lead for Mark
    Smith") must never be labeled like a scheduled lead follow-up on the
    calendar, even if its task_type happens to be send_sms/call/etc -- that
    reads as a real /crm/follow-ups item when it isn't one."""
    u1, _ = two_users
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    task_id, err = crm_db.create_task(
        u1,
        {
            "title": "Create a lead for Mark Smith",
            "due_at": due,
            "task_type": "send_sms",
        },
    )
    assert err is None
    events = [e for e in crm_db.list_calendar_events(u1) if e["id"] == f"task:{task_id}"]
    assert len(events) == 1
    assert events[0]["event_type"] == "task"
    assert events[0]["lead_id"] is None


def test_task_with_lead_still_maps_to_typed_event(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "Typed Task Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    task_id, err = crm_db.create_task(
        u1,
        {
            "title": "Call the lead",
            "lead_id": lead_id,
            "due_at": due,
            "task_type": "call",
        },
    )
    assert err is None
    events = [e for e in crm_db.list_calendar_events(u1) if e["id"] == f"task:{task_id}"]
    assert len(events) == 1
    assert events[0]["event_type"] == "call"


def test_timezone_local_day_helper(two_users):
    # 02:00 UTC on Jul 30 is Jul 29 evening in America/Denver.
    due = "2026-07-30T02:00:00+00:00"
    assert crm_db._local_date_for_due(due, timezone_name="America/Denver") == "2026-07-29"
    assert crm_db._local_date_for_due(due, timezone_name="UTC") == "2026-07-30"
    # Legacy fixed-offset path remains for display helpers.
    assert crm_db._local_date_for_due(due, tz_offset_minutes=360) == "2026-07-29"
    assert crm_db._local_date_for_due(due, tz_offset_minutes=0) == "2026-07-30"


def test_calendar_page_and_api_tenant_isolation(app_client, two_users):
    u1, u2 = two_users
    lead_id = _lead(u1, "Private Cal Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    crm_db.set_lead_follow_up(u1, lead_id, due, "Private")
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    page = app_client.get("/crm/calendar")
    assert page.status_code == 200
    assert "Private Cal Lead" in page.get_data(as_text=True)
    api = app_client.get("/api/crm/calendar/events")
    assert api.status_code == 200
    assert any(e["lead_id"] == lead_id for e in api.get_json()["events"])

    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    page2 = app_client.get("/crm/calendar")
    assert "Private Cal Lead" not in page2.get_data(as_text=True)
    api2 = app_client.get("/api/crm/calendar/events")
    assert not any(e["lead_id"] == lead_id for e in api2.get_json()["events"])


def test_duplicate_follow_up_cleanup_dry_run_and_execute(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        second=0, microsecond=0
    ).isoformat()
    # Force-create duplicates with same reason/due.
    a, _ = crm_db.set_lead_follow_up(u1, lead_id, due, "Follow up", force_create=True)
    b, _ = crm_db.set_lead_follow_up(u1, lead_id, due, "Follow up", force_create=True)
    c, _ = crm_db.set_lead_follow_up(u1, lead_id, due, "Follow up", force_create=True)
    assert len({a["follow_up_id"], b["follow_up_id"], c["follow_up_id"]}) == 3

    dry = crm_db.find_duplicate_open_follow_ups(u1, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["duplicate_count"] >= 2
    assert dry["cancelled_ids"] == []

    executed = crm_db.find_duplicate_open_follow_ups(u1, dry_run=False)
    assert executed["dry_run"] is False
    assert executed["cancelled_ids"]
    open_items = [
        f
        for f in crm_db.list_lead_follow_ups(u1, lead_id)
        if f["status"] == "pending"
        and crm_db.normalize_follow_up_reason(f.get("reason")) == "follow up"
        and crm_db._normalize_due_at_key(f.get("due_at"))
        == crm_db._normalize_due_at_key(due)
    ]
    assert len(open_items) == 1
    # Cancelled rows still exist.
    with db.get_db() as conn:
        cancelled = conn.execute(
            """
            SELECT COUNT(*) AS count FROM lead_follow_ups
            WHERE user_id = ? AND lead_id = ? AND status = 'cancelled'
            """,
            (u1, lead_id),
        ).fetchone()["count"]
    assert cancelled >= 2


def test_lead_detail_links_to_calendar(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.get(f"/crm/leads/{lead_id}")
    html = res.get_data(as_text=True)
    assert "View in Leads Calendar" in html
    assert "/crm/calendar" in html
