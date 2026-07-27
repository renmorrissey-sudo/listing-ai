"""Follow-up cancellation requires a reason and preserves history."""

import uuid
from datetime import datetime, timedelta, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations


def _lead(user_id):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    return db.upsert_lead(
        user_id, phone, {"name": "Cancel Lead", "lead_type": "buyer"}, source="sms"
    )


def _schedule(user_id, lead_id, reason="Call back"):
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result, err = crm_db.set_lead_follow_up(user_id, lead_id, due, reason)
    assert err is None
    return result["follow_up_id"], due


def test_cancellation_requires_reason(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    fid, _ = _schedule(u1, lead_id)
    result, err = crm_db.cancel_follow_up(u1, fid, cancel_reason_code="")
    assert result is None
    assert "required" in err.lower()


def test_other_requires_notes(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    fid, _ = _schedule(u1, lead_id)
    result, err = crm_db.cancel_follow_up(u1, fid, cancel_reason_code="other")
    assert result is None
    assert "explain" in err.lower()
    result, err = crm_db.cancel_follow_up(
        u1, fid, cancel_reason_code="other", cancel_reason_notes="Wrong lead"
    )
    assert err is None
    assert result["status"] == "cancelled"


def test_cancelled_leaves_active_lists_and_remains_in_history(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    fid, due = _schedule(u1, lead_id, "Buyer consultation")
    crm_db.cancel_follow_up(
        u1, fid, cancel_reason_code="duplicate_follow_up", cancel_reason_notes=""
    )
    groups = crm_db.group_follow_ups_for_lead(
        crm_db.list_lead_follow_ups(u1, lead_id),
        local_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    assert groups["overdue"] == []
    assert groups["today"] == []
    assert groups["upcoming"] == []
    assert len(groups["cancelled"]) == 1
    item = groups["cancelled"][0]
    assert item["id"] == fid
    assert item["due_at"] == due
    assert item["cancel_reason_code"] == "duplicate_follow_up"
    assert item["cancelled_at"]
    # Row still exists in DB.
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_follow_ups WHERE id = ? AND user_id = ?",
            (fid, u1),
        ).fetchone()
    assert row is not None
    assert dict(row)["status"] == "cancelled"


def test_activity_timeline_records_reason(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    fid, _ = _schedule(u1, lead_id)
    crm_db.cancel_follow_up(u1, fid, cancel_reason_code="no_longer_needed")
    acts = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=True)
        if a["event_type"] == "follow_up_cancelled"
    ]
    assert len(acts) == 1
    assert "No longer needed" in acts[0]["summary"]


def test_duplicate_cancel_is_idempotent(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    fid, _ = _schedule(u1, lead_id)
    first, err = crm_db.cancel_follow_up(u1, fid, cancel_reason_code="created_by_mistake")
    assert err is None and first["duplicate"] is False
    second, err = crm_db.cancel_follow_up(u1, fid, cancel_reason_code="created_by_mistake")
    assert err is None and second["duplicate"] is True
    acts = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
        if a["event_type"] == "follow_up_cancelled"
    ]
    assert len(acts) == 1


def test_cross_tenant_cancel_blocked(two_users, app_client):
    u1, u2 = two_users
    lead_id = _lead(u1)
    fid, _ = _schedule(u1, lead_id)
    result, err = crm_db.cancel_follow_up(u2, fid, cancel_reason_code="no_longer_needed")
    assert result is None and err == "Follow-up not found."
    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    res = app_client.post(
        f"/api/crm/follow-ups/{fid}/cancel",
        json={"cancel_reason_code": "no_longer_needed"},
    )
    assert res.status_code == 404
    assert crm_db.get_follow_up(u1, fid)["status"] == "pending"


def test_no_further_contact_offers_dnc_but_does_not_opt_out(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    fid, _ = _schedule(u1, lead_id)
    result, err = crm_db.cancel_follow_up(
        u1, fid, cancel_reason_code="lead_requested_no_further_contact"
    )
    assert err is None
    assert result["offer_dnc"] is True
    lead = db.get_lead(lead_id, u1)
    assert lead.get("opt_out_status") != "opted_out"
    assert lead.get("status") != "do_not_contact"


def test_api_cancel_requires_reason(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    fid, _ = _schedule(u1, lead_id)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(f"/api/crm/follow-ups/{fid}/cancel", json={})
    assert res.status_code == 400
