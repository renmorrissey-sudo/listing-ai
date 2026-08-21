"""Autonomous TopAI CRM, SMS, and scheduling workflows."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import uuid

import autonomy
import crm_db
import db
import scheduling
import sms_ai_agent
import sms_coach
from ask_topai import actions, policy, service
from migrations.runner import apply_pending_migrations
from tests.test_telnyx_inbound_ai_workflow import (
    _analysis,
    _lead as _sms_lead,
    _setup_tenant,
    _unique_e164,
)


def _lead(user_id, name="Sarah Johnson", phone=None):
    apply_pending_migrations()
    phone = phone or f"+1555{uuid.uuid4().hex[:7]}"
    return db.upsert_lead(user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms")


def _provider():
    class _P:
        def send_sms(self, *args, **kwargs):
            return {"provider_message_id": f"tx-{uuid.uuid4().hex[:8]}", "status": "queued"}

    return _P()


def test_policy_auto_execute_vs_block():
    assert policy.confirmation_mode("add_lead_note") == policy.MODE_AUTO
    assert policy.confirmation_mode("create_calendar_event") == policy.MODE_AUTO
    assert policy.confirmation_mode("create_follow_up") == policy.MODE_AUTO
    assert policy.confirmation_mode("send_email") == policy.MODE_SPOKEN_CONFIRMATION
    assert policy.confirmation_mode("delete_lead") == policy.MODE_SPOKEN_CONFIRMATION
    assert autonomy.should_auto_execute_tool("create_task") is True
    assert autonomy.allowed_auto_status("closed_lost") is None
    assert autonomy.allowed_auto_status("appointment_scheduled") == "appointment_scheduled"


def test_inbound_ai_sends_automatically_without_review(two_users, monkeypatch):
    u1, u2 = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("303")
    lead_id, _ = _sms_lead(u1, contact, name="Sarah Johnson")
    inbound_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": "Sarah Johnson",
            "phone_number": contact,
            "message_body": "Can we see the property Saturday?",
        },
        status="received",
        lead_id=lead_id,
        direction="inbound",
    )
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch("sms_providers.get_sms_provider", return_value=_provider()):
        outcome = sms_ai_agent.process_inbound_ai(
            u1, lead_id, inbound_id, "Can we see the property Saturday?", account
        )
    assert outcome["replied"] is True
    assert all(i["lead_id"] != lead_id for i in db.list_pending_insights(u1))
    suggested = [
        m for m in db.list_lead_messages(u1, lead_id) if m["direction"] == "suggested"
    ]
    assert suggested == []
    outbound = [
        m
        for m in db.list_lead_messages(u1, lead_id)
        if m["direction"] == "outbound" and m.get("reply_to_message_id") == inbound_id
    ]
    assert len(outbound) == 1
    assert db.get_lead(lead_id, u2) is None
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "sms_ai_reply" for a in activities)


def test_opt_out_blocks_automatic_reply(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("304")
    lead_id, _ = _sms_lead(u1, contact)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET opt_out_status = 'opted_out' WHERE id = ?",
            (lead_id,),
        )
    inbound_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Lead", "phone_number": contact, "message_body": "Hi"},
        status="received",
        lead_id=lead_id,
        direction="inbound",
    )
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch("sms_providers.get_sms_provider", return_value=_provider()) as send:
        outcome = sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Hi", account)
    assert outcome["replied"] is False
    assert outcome["reason"] == "opted_out"
    send.assert_not_called()


def test_quiet_hours_defer_not_review(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("305")
    lead_id, _ = _sms_lead(u1, contact)
    inbound_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Lead", "phone_number": contact, "message_body": "Hello"},
        status="received",
        lead_id=lead_id,
        direction="inbound",
    )
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch("sms_quiet_hours.in_quiet_hours", return_value=True), \
         patch(
             "sms_quiet_hours.next_permitted_send_at",
             return_value=datetime.now(timezone.utc) + timedelta(hours=8),
         ), \
         patch("sms_providers.get_sms_provider", return_value=_provider()) as send:
        outcome = sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Hello", account)
    assert outcome.get("scheduled") is True
    assert outcome["replied"] is True
    send.assert_not_called()
    assert all(i["lead_id"] != lead_id for i in db.list_pending_insights(u1))


def test_ask_topai_creates_note_task_criteria(two_users):
    u1, u2 = two_users
    lead_id = _lead(u1, "Sarah Johnson")
    note, err, _ = actions.execute_add_note(
        u1, {"lead_id": lead_id, "note": "Looking up to $900,000 now."}, {}
    )
    assert err is None
    assert "900,000" in (note.get("notes") or "")
    task, err, _ = actions.execute_create_task(
        u1,
        {"lead_id": lead_id, "title": "Call Ryan", "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        {},
    )
    assert err is None
    assert task["title"] == "Call Ryan"
    updated, err, _ = actions.execute_update_criteria(
        u1, {"lead_id": lead_id, "price_max": 900000}, {}
    )
    assert err is None
    _other, err, _ = actions.execute_add_note(
        u2, {"lead_id": lead_id, "note": "should not write"}, {}
    )
    assert err
    assert "should not write" not in (db.get_lead(lead_id, u1).get("notes") or "")


def test_ask_topai_ambiguous_lead_clarifies(two_users):
    u1, _ = two_users
    _lead(u1, "John Smith", phone="+15551110001")
    _lead(u1, "John Smith", phone="+15551110002")
    lead, err, choices = actions.resolve_lead(u1, {"lead_name": "John Smith"}, {})
    assert lead is None
    assert "which" in (err or "").lower() or "multiple" in (err or "").lower()
    assert len(choices) >= 2


def test_ai_captures_lead_context(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "Michael")
    autonomy.apply_inbound_side_effects(
        u1,
        lead_id,
        {
            "captured_note": "Needs a finished basement because parents visit often.",
            "property_criteria_updates": {"price_max": 925000, "neighborhood": "Highlands Ranch"},
            "suggested_lead_status": "qualified",
            "suggested_tasks": [{"title": "Prepare a CMA for Michael"}],
        },
    )
    lead = db.get_lead(lead_id, u1)
    assert "finished basement" in (lead.get("notes") or "")
    assert lead["status"] == "qualified"
    tasks = [t for t in crm_db.list_tasks(u1) if t.get("lead_id") == lead_id]
    assert any("CMA" in (t.get("title") or "") for t in tasks)
    autonomy.apply_inbound_side_effects(
        u1,
        lead_id,
        {"suggested_tasks": [{"title": "Prepare a CMA for Michael"}]},
    )
    tasks2 = [t for t in crm_db.list_tasks(u1) if t.get("lead_id") == lead_id]
    assert len([t for t in tasks2 if "CMA" in (t.get("title") or "")]) == 1


def test_calendar_availability_and_schedule(two_users):
    u1, _ = two_users
    apply_pending_migrations()
    lead_id = _lead(u1, "Sarah Johnson")
    slots = scheduling.find_available_slots(u1, limit=4)
    assert slots
    first = slots[0]
    busy_id, err = crm_db.create_appointment(
        u1,
        {
            "lead_id": lead_id,
            "start_at": first["start_at"],
            "end_at": first["end_at"],
            "appointment_type": "phone_call",
        },
    )
    assert err is None
    availability = scheduling.get_calendar_availability(
        u1, start_at=first["start_at"], end_at=first["end_at"]
    )
    assert availability["busy"]
    dup, err, alternatives = scheduling.create_calendar_event(
        u1, {"lead_id": lead_id, "start_at": first["start_at"], "end_at": first["end_at"]}
    )
    assert dup is None
    assert err

    lead2 = _lead(u1, "Ryan")
    other = scheduling.find_available_slots(u1, limit=3)
    assert other
    booked, err, _ = scheduling.create_calendar_event(
        u1, {"lead_id": lead2, "start_at": other[0]["start_at"], "end_at": other[0]["end_at"]}
    )
    assert err is None
    assert booked["id"] != busy_id
    assert db.get_lead(lead2, u1)["status"] == "appointment_scheduled"


def test_reschedule_updates_existing_not_duplicate(two_users):
    u1, _ = two_users
    apply_pending_migrations()
    lead_id = _lead(u1, "Sarah Johnson")
    slots = scheduling.find_available_slots(u1, limit=5)
    first, second = slots[0], slots[2]
    created, err, _ = scheduling.create_calendar_event(
        u1, {"lead_id": lead_id, "start_at": first["start_at"], "end_at": first["end_at"]}
    )
    assert err is None
    updated, err, _ = scheduling.reschedule_calendar_event(
        u1, created["id"], {"start_at": second["start_at"], "end_at": second["end_at"]}
    )
    assert err is None
    assert updated["id"] == created["id"]
    assert updated["start_at"] == second["start_at"]
    appts = [
        a
        for a in crm_db.list_appointments(u1, lead_id=lead_id)
        if a.get("status") in scheduling.ACTIVE_APPT_STATUSES
    ]
    assert len(appts) == 1
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "appointment_rescheduled" for a in activities)


def test_timezone_slots_use_account_zone(two_users):
    u1, _ = two_users
    apply_pending_migrations()
    slots = scheduling.find_available_slots(u1, limit=1)
    assert slots
    assert slots[0]["timezone"]


def test_no_routine_needs_your_review_copy(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    html = app_client.get("/app").get_data(as_text=True)
    assert "Needs your review" not in html
    assert "Approve &amp; Send" not in html
    dash = app_client.get("/dashboard").get_data(as_text=True)
    assert "Drafts awaiting" not in dash
    assert "AI actions completed" in dash


def test_ask_topai_interpret_executes_note(two_users, monkeypatch):
    u1, _ = two_users
    lead_id = _lead(u1, "Sarah")
    monkeypatch.setattr(
        "ask_topai.agent.complete",
        lambda *args, **kwargs: {
            "status": "ok",
            "message": "Done.",
            "commands": [
                {
                    "action": "add_lead_note",
                    "arguments": {"lead_id": lead_id, "note": "Budget is 900000"},
                }
            ],
            "tools_invoked": ["add_lead_note"],
            "session_id": "s1",
            "source": "text",
            "model": "test",
            "choices": [],
            "grounding_transcript": "Add a note to Sarah that she is looking up to 900000",
        },
    )
    result = service.interpret(
        u1,
        "Add a note to Sarah that she is looking up to 900000",
        {"lead_id": lead_id},
    )
    assert result["status"] == "executed"
    assert result["confirmation_token"] is None
    assert "900000" in (db.get_lead(lead_id, u1).get("notes") or "")
