"""Appointment outcome suggestions require agent approval before applying."""

import uuid
from datetime import datetime, timedelta, timezone

import crm_db
import db
from crm_constants import (
    APPOINTMENT_OUTCOME_SUGGESTIONS,
    APPOINTMENT_OUTCOMES,
    build_appointment_outcome_suggestion,
    outcome_label,
)
from migrations.runner import apply_pending_migrations


def _lead(user_id, status="attempting_contact"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    lead_id = db.upsert_lead(
        user_id, phone, {"name": "Appt Lead", "lead_type": "buyer"}, source="sms"
    )
    crm_db.set_lead_status(user_id, lead_id, status)
    return lead_id


def _appt(user_id, lead_id):
    start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    appt_id, err = crm_db.create_appointment(
        user_id,
        {
            "lead_id": lead_id,
            "appointment_type": "buyer_consultation",
            "start_at": start,
            "end_at": start,
        },
    )
    assert err is None
    return appt_id


def test_each_outcome_maps_to_expected_lead_status():
    expected = {
        "qualified_opportunity": "qualified",
        "follow_up_required": "contacted",
        "showing_scheduled": "appointment_scheduled",
        "listing_appointment_scheduled": "appointment_scheduled",
        "buyer_agreement_signed": "under_contract",
        "listing_agreement_signed": "under_contract",
        "not_ready": "nurture",
        "not_qualified": "closed_lost",
        "lost_to_another_agent": "closed_lost",
        "no_response": "attempting_contact",
        "no_show": "attempting_contact",
        "other": "appointment_completed",
    }
    assert set(expected) == set(APPOINTMENT_OUTCOMES)
    assert set(expected) == set(APPOINTMENT_OUTCOME_SUGGESTIONS)
    for outcome, status in expected.items():
        suggestion = build_appointment_outcome_suggestion(
            outcome, current_lead_status="new"
        )
        assert suggestion is not None
        assert suggestion["suggested_lead_status"] == status
        assert suggestion["outcome_label"] == outcome_label(outcome)
        assert suggestion["lead_status_would_change"] is True
        # Qualified Opportunity must never suggest Appointment Scheduled.
        if outcome == "qualified_opportunity":
            assert status == "qualified"
            assert status != "appointment_scheduled"


def test_qualified_opportunity_does_not_auto_change_lead_status(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "attempting_contact")
    # Mimic scheduling an appointment which sets Appointment Scheduled.
    crm_db.set_lead_status(u1, lead_id, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)

    result, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", apply_lead_status=False
    )
    assert err is None
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "appointment_scheduled"
    assert result["confirmation"].startswith("Outcome saved")

    result2, err = crm_db.record_appointment_outcome(
        u1,
        appt_id,
        "qualified_opportunity",
        apply_lead_status=True,
        apply_follow_up=True,
    )
    assert err is None
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "qualified"
    assert lead.get("next_action")
    assert lead.get("next_follow_up_at")
    assert "Qualified" in result2["confirmation"]
    assert "Follow-up scheduled" in result2["confirmation"]


def test_outcome_preview_and_idempotent_save(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "attempting_contact")
    appt_id = _appt(u1, lead_id)
    preview, err = crm_db.preview_appointment_outcome(u1, appt_id, "qualified_opportunity")
    assert err is None
    assert preview["suggested_lead_status"] == "qualified"
    assert preview["suggested_next_action"]

    first, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", outcome_notes="Great conversation"
    )
    assert err is None and first["duplicate"] is False
    second, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", outcome_notes="Great conversation"
    )
    assert err is None and second["duplicate"] is True
    activities = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
        if a["event_type"] == "appointment_outcome"
    ]
    assert len(activities) == 1
    assert "Appointment outcome saved: Qualified Opportunity" in activities[0]["summary"]


def test_approved_status_change_writes_descriptive_timeline(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "attempting_contact")
    appt_id = _appt(u1, lead_id)
    result, err = crm_db.record_appointment_outcome(
        u1, appt_id, "not_qualified", apply_lead_status=True
    )
    assert err is None
    assert db.get_lead(lead_id, u1)["status"] == "closed_lost"
    activities = crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
    summaries = [a["summary"] for a in activities]
    assert any(s.startswith("Appointment outcome saved: Not Qualified") for s in summaries)
    assert any(
        "Lead status changed from Attempting Contact to Closed Lost" in s for s in summaries
    )
    assert "Closed Lost" in result["confirmation"]


def test_no_show_can_open_needs_attention_when_approved(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    result, err = crm_db.record_appointment_outcome(
        u1,
        appt_id,
        "no_show",
        apply_lead_status=True,
        apply_needs_attention=True,
    )
    assert err is None
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "attempting_contact"
    items = crm_db.list_needs_attention(u1)
    assert any(i["reason_code"] == "appointment_no_show" for i in items)
    assert "Needs Attention" in result["confirmation"]


def test_api_preview_and_save_require_auth_and_ownership(app_client, two_users):
    u1, u2 = two_users
    lead_id = _lead(u1, "attempting_contact")
    appt_id = _appt(u1, lead_id)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    preview = app_client.post(
        f"/api/crm/appointments/{appt_id}/outcome-preview",
        json={"outcome": "showing_scheduled"},
    )
    assert preview.status_code == 200
    body = preview.get_json()["preview"]
    assert body["suggested_lead_status"] == "appointment_scheduled"

    save = app_client.post(
        f"/api/crm/appointments/{appt_id}/outcome",
        json={
            "outcome": "qualified_opportunity",
            "apply_lead_status": True,
            "apply_follow_up": True,
        },
    )
    assert save.status_code == 200
    assert "Qualified" in save.get_json()["confirmation"]

    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    denied = app_client.post(
        f"/api/crm/appointments/{appt_id}/outcome",
        json={"outcome": "not_ready", "apply_lead_status": True},
    )
    assert denied.status_code == 400
    assert db.get_lead(lead_id, u1)["status"] == "qualified"
