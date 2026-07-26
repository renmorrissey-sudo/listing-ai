from datetime import datetime, timedelta, timezone

import crm_db
import db
from crm_constants import normalize_lead_status


def _lead(user_id, phone="+15551110001"):
    return db.upsert_lead(user_id, phone, {"name": "Test Lead", "lead_type": "buyer"}, source="sms")


def test_user_isolation(two_users):
    u1, u2 = two_users
    lead1 = _lead(u1, "+15551110001")
    lead2 = _lead(u2, "+15551110002")
    assert db.get_lead(lead1, u2) is None
    assert db.get_lead(lead2, u1) is None
    assert crm_db.filter_leads(u1)[0]["id"] == lead1
    assert all(l["id"] != lead1 for l in crm_db.filter_leads(u2))


def test_status_change_and_timeline(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    lead, err = crm_db.set_lead_status(u1, lead_id, "qualified")
    assert err is None
    assert normalize_lead_status(lead["status"]) == "qualified"
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "status_change" for a in activities)


def test_dnc_protected_from_automation(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    crm_db.set_lead_status(u1, lead_id, "do_not_contact")
    lead, err = crm_db.set_lead_status(u1, lead_id, "contacted", from_automation=True)
    assert lead is None
    assert "Do Not Contact" in err
    # Agent can still change manually
    lead, err = crm_db.set_lead_status(u1, lead_id, "contacted", from_automation=False)
    assert err is None
    assert normalize_lead_status(lead["status"]) == "contacted"


def test_follow_up_complete(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    fid, err = crm_db.set_lead_follow_up(u1, lead_id, due, "Call back")
    assert err is None and fid
    ok, err = crm_db.complete_lead_follow_up(u1, lead_id)
    assert ok and err is None
    lead = db.get_lead(lead_id, u1)
    assert lead["next_follow_up_at"] is None


def test_task_complete_creates_activity(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    task_id, err = crm_db.create_task(u1, {"title": "Send CMA", "lead_id": lead_id, "task_type": "prepare_cma"})
    assert err is None
    crm_db.complete_task(u1, task_id)
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "task_completed" for a in activities)


def test_appointment_outcome_required_for_resolve_path(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    appt_id, err = crm_db.create_appointment(
        u1,
        {
            "lead_id": lead_id,
            "appointment_type": "phone_call",
            "start_at": past,
            "end_at": past,
        },
    )
    assert err is None
    crm_db.refresh_needs_attention(u1)
    items = crm_db.list_needs_attention(u1)
    assert any(i["reason_code"] == "appointment_outcome_missing" for i in items)
    ok, err = crm_db.record_appointment_outcome(u1, appt_id, "follow_up_required")
    assert ok and err is None
    items = [i for i in crm_db.list_needs_attention(u1) if i["reason_code"] == "appointment_outcome_missing"]
    assert items == []


def test_needs_attention_resolve_requires_reason_for_opt_out(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    item_id = crm_db.upsert_needs_attention(u1, lead_id, "opt_out", priority="urgent")
    ok, err = crm_db.resolve_needs_attention(u1, item_id, "")
    assert ok is None
    assert "resolution reason" in err.lower()
    ok, err = crm_db.resolve_needs_attention(u1, item_id, "Confirmed STOP keyword")
    assert ok is True


def test_opt_out_cancels_suggested_drafts(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    msg_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="twilio",
        data={
            "lead_name": "Test",
            "phone_number": "+15551110001",
            "message_body": "Draft reply",
        },
        status="suggested",
        lead_id=lead_id,
        direction="suggested",
    )
    db.mark_lead_opt_out(lead_id, u1)
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "do_not_contact"
    assert lead["opt_out_status"] == "opted_out"
    with db.get_db() as conn:
        row = conn.execute("SELECT status FROM sms_messages WHERE id = ?", (msg_id,)).fetchone()
    assert row["status"] == "cancelled"


def test_pipeline_metrics_smoke(two_users):
    u1, _ = two_users
    _lead(u1)
    metrics = crm_db.get_pipeline_metrics(u1)
    assert "active_leads" in metrics
    assert "pipeline_stages" in metrics
    assert isinstance(metrics["pipeline_stages"], list)
    assert metrics["active_leads"] >= 1


def test_api_status_requires_login(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    res = app_client.post(f"/api/crm/leads/{lead_id}/status", json={"status": "qualified"})
    assert res.status_code == 401


def test_api_status_and_isolation(app_client, two_users):
    u1, u2 = two_users
    lead1 = _lead(u1, "+15552220001")
    lead2 = _lead(u2, "+15552220002")

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    res = app_client.post(f"/api/crm/leads/{lead1}/status", json={"status": "nurture"})
    assert res.status_code == 200
    assert normalize_lead_status(res.get_json()["lead"]["status"]) == "nurture"

    res = app_client.post(f"/api/crm/leads/{lead2}/status", json={"status": "qualified"})
    assert res.status_code in (400, 404)
