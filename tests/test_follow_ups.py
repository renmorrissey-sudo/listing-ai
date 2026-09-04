"""Follow-up scheduling, dedupe, calendar, and tenant isolation."""

import uuid
from datetime import datetime, timedelta, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations


def _lead(user_id, name="Follow Lead"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    return db.upsert_lead(
        user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms"
    )


def _open_follow_ups(user_id, lead_id):
    return [
        f
        for f in crm_db.list_lead_follow_ups(user_id, lead_id, include_completed=True)
        if f["status"] == "pending"
    ]


def test_quick_schedule_creates_visible_follow_up(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    ).isoformat()
    result, err = crm_db.set_lead_follow_up(
        u1,
        lead_id,
        due,
        "Buyer consultation",
        local_due_label="July 30, 2026 at 9:00 AM",
    )
    assert err is None
    assert result["created"] is True
    assert result["follow_up_id"]
    items = _open_follow_ups(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason"] == "Buyer consultation"
    lead = db.get_lead(lead_id, u1)
    assert lead["next_follow_up_at"] == due
    assert "July 30, 2026 at 9:00 AM" in result["confirmation"]
    assert "Buyer consultation" in result["confirmation"]


def test_duplicate_clicks_do_not_create_duplicate_open_follow_ups(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    first, err = crm_db.set_lead_follow_up(u1, lead_id, due, "Follow up")
    assert err is None
    second, err = crm_db.set_lead_follow_up(u1, lead_id, due, "Follow up")
    assert err is None
    assert second["duplicate"] is True
    assert second["follow_up_id"] == first["follow_up_id"]
    assert len(_open_follow_ups(u1, lead_id)) == 1
    activities = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=True)
        if a["event_type"] == "follow_up_scheduled"
    ]
    assert len(activities) == 1


def test_reschedule_updates_existing_record(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due1 = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    due2 = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    first, err = crm_db.set_lead_follow_up(u1, lead_id, due1, "Call back")
    assert err is None
    second, err = crm_db.set_lead_follow_up(
        u1, lead_id, due2, "Call back", replace_existing=True
    )
    assert err is None
    assert second["updated"] is True
    assert second["follow_up_id"] == first["follow_up_id"]
    items = _open_follow_ups(u1, lead_id)
    assert len(items) == 1
    assert items[0]["due_at"] == due2
    activities = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=True)
        if a["event_type"] == "follow_up_scheduled"
    ]
    assert len(activities) == 1


def test_completing_moves_to_completed(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result, err = crm_db.set_lead_follow_up(u1, lead_id, due, "Tour follow-up")
    assert err is None
    ok, err = crm_db.complete_follow_up(u1, result["follow_up_id"])
    assert ok and err is None
    groups = crm_db.group_follow_ups_for_lead(
        crm_db.list_lead_follow_ups(u1, lead_id),
        local_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    assert groups["upcoming"] == []
    assert groups["today"] == []
    assert groups["overdue"] == []
    assert len(groups["completed"]) == 1
    assert groups["completed"][0]["status"] == "done"
    assert db.get_lead(lead_id, u1)["next_follow_up_at"] is None


def test_follow_ups_appear_on_lead_detail_and_calendar(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1, name="Calendar Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        hour=16, minute=0, second=0, microsecond=0
    ).isoformat()
    crm_db.set_lead_follow_up(u1, lead_id, due, "Schedule buyer consultation")
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    detail = app_client.get(f"/crm/leads/{lead_id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert "Schedule buyer consultation" in html
    assert "Next Actions to do" in html
    assert "Follow-up to do" in html
    assert "Schedule snapshot" not in html
    assert "Follow-ups" in html

    calendar = app_client.get("/crm/follow-ups")
    assert calendar.status_code == 200
    cal_html = calendar.get_data(as_text=True)
    assert "Calendar Lead" in cal_html
    assert "Schedule buyer consultation" in cal_html

    api = app_client.get("/api/crm/follow-ups?bucket=upcoming")
    assert api.status_code == 200
    body = api.get_json()
    assert any(i["lead_id"] == lead_id for i in body["follow_ups"])


def test_follow_ups_page_copy_has_no_database_table_name(app_client, two_users):
    u1, _ = two_users
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    page = app_client.get("/crm/follow-ups")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "lead_follow_ups" not in html
    assert "not from the activity timeline" not in html


def test_timezone_local_date_bucketing(two_users):
    u1, _ = two_users
    db.update_business_profile(u1, timezone="America/Denver")
    lead_id = _lead(u1)
    # 2026-07-30 02:00 UTC is still 2026-07-29 evening in America/Denver.
    due = "2026-07-30T02:00:00+00:00"
    crm_db.set_lead_follow_up(u1, lead_id, due, "Evening call")
    items = crm_db.list_lead_follow_ups(u1, lead_id)
    now = datetime(2026, 7, 30, 17, 0, tzinfo=timezone.utc)
    groups_denver = crm_db.group_follow_ups_for_lead(
        items, timezone_name="America/Denver", now=now, user_id=u1
    )
    groups_utc = crm_db.group_follow_ups_for_lead(
        items, timezone_name="UTC", now=now, user_id=u1
    )
    assert len(groups_denver["overdue"]) == 1
    assert len(groups_utc["today"]) == 1
    assert crm_db._local_date_for_due(due, timezone_name="America/Denver") == "2026-07-29"
    assert crm_db._local_date_for_due(due, tz_offset_minutes=360) == "2026-07-29"


def test_cross_tenant_cannot_view_or_modify(two_users, app_client):
    u1, u2 = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result, err = crm_db.set_lead_follow_up(u1, lead_id, due, "Private follow-up")
    assert err is None
    fid = result["follow_up_id"]

    assert crm_db.get_follow_up(u2, fid) is None
    updated, error = crm_db.update_follow_up(u2, fid, reason="Hijack")
    assert updated is None and error == "Follow-up not found."
    ok, error = crm_db.complete_follow_up(u2, fid)
    assert ok is False and error == "Follow-up not found."

    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    denied = app_client.patch(
        f"/api/crm/follow-ups/{fid}",
        json={"reason": "Nope"},
    )
    assert denied.status_code == 404
    assert crm_db.get_follow_up(u1, fid)["reason"] == "Private follow-up"


def test_activity_timeline_one_event_per_action(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due1 = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    due2 = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    crm_db.set_lead_follow_up(u1, lead_id, due1, "Follow up", local_due_label="Day One")
    crm_db.set_lead_follow_up(u1, lead_id, due1, "Follow up", local_due_label="Day One")
    crm_db.set_lead_follow_up(u1, lead_id, due2, "Follow up", local_due_label="Day Five")
    timeline = crm_db.list_lead_activities(u1, lead_id, for_timeline=True)
    scheduled = [a for a in timeline if a["event_type"] == "follow_up_scheduled"]
    assert len(scheduled) == 1
    assert "Day Five" in scheduled[0]["summary"]


def test_api_quick_follow_up_confirmation(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    ).isoformat()
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/follow-up",
        json={
            "due_at": due,
            "reason": "Buyer consultation",
            "local_due_label": "July 30, 2026 at 9:00 AM",
            "replace_existing": True,
        },
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert "July 30, 2026 at 9:00 AM" in body["confirmation"]
    # Second identical click is idempotent.
    res2 = app_client.post(
        f"/api/crm/leads/{lead_id}/follow-up",
        json={
            "due_at": due,
            "reason": "Buyer consultation",
            "local_due_label": "July 30, 2026 at 9:00 AM",
            "replace_existing": True,
        },
    )
    assert res2.status_code == 200
    assert res2.get_json()["duplicate"] is True
    assert len(_open_follow_ups(u1, lead_id)) == 1


def test_force_create_allows_second_open_follow_up(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due1 = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    due2 = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    crm_db.set_lead_follow_up(u1, lead_id, due1, "Follow up")
    result, err = crm_db.set_lead_follow_up(
        u1, lead_id, due2, "Follow up", force_create=True
    )
    assert err is None
    assert result["created"] is True
    assert len(_open_follow_ups(u1, lead_id)) == 2
