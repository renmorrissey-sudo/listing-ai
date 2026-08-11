"""AI SMS coaching must persist a real lead_follow_ups record whenever Claude
determines a concrete future action -- not just a freeform next_action string.

Mirrors the voice-call path (lead_service.ensure_lead_follow_through), which
already creates a real follow-up via crm_db.set_lead_follow_up.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crm_db
import db
import sms_coach
import sms_inbound
from migrations.runner import apply_pending_migrations


def _lead(user_id, name="Coach Lead"):
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


def _analysis(**overrides):
    base = {
        "summary": "Lead asked to be contacted later.",
        "intent": "timing",
        "recommended_next_action": "Confirm the reschedule with the lead.",
        "draft_reply": "Sounds good, I'll follow up then!",
        "confidence": 0.9,
        "confidence_score": 0.9,
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "suggested_lead_status": "contacted",
        "suggested_follow_up_at": None,
        "suggested_follow_up_reason": "",
        "suggested_tasks": [],
        "appointment_requested": False,
        "appointment_details": None,
        "needs_attention_reasons": [],
        "next_best_step": "Confirm the reschedule with the lead.",
        "recommended_action": "Confirm the reschedule with the lead.",
        "suggested_reply": "Sounds good, I'll follow up then!",
        "home_value_pitch": None,
        "raw_json": None,
    }
    base.update(overrides)
    return base


def test_coach_with_suggested_follow_up_creates_real_follow_up(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "Sarah Johnson")
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    ).isoformat()
    analysis = _analysis(
        suggested_follow_up_at=due,
        suggested_follow_up_reason="Confirm the reschedule with Sarah",
    )
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=analysis):
        result = sms_inbound.analyze_inbound_and_coach(
            u1, lead_id, None, "Can you text me tomorrow afternoon?"
        )
    assert result["error"] is None

    items = _open_follow_ups(u1, lead_id)
    assert len(items) == 1
    assert items[0]["due_at"] == due
    assert items[0]["reason"] == "Confirm the reschedule with Sarah"

    lead = db.get_lead(lead_id, u1)
    assert lead["next_follow_up_at"] == due
    assert lead["next_action"] == "Confirm the reschedule with the lead."

    # And it shows up on the calendar/follow-ups aggregation, same as voice.
    events = crm_db.list_calendar_events(u1)
    assert any(
        e["event_type"] == "follow_up" and e["lead_id"] == lead_id for e in events
    )


def test_coach_without_future_date_does_not_create_follow_up(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    analysis = _analysis()  # suggested_follow_up_at stays None
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=analysis):
        result = sms_inbound.analyze_inbound_and_coach(
            u1, lead_id, None, "Thanks, talk soon."
        )
    assert result["error"] is None
    assert _open_follow_ups(u1, lead_id) == []
    lead = db.get_lead(lead_id, u1)
    assert lead["next_action"] == "Confirm the reschedule with the lead."
    assert not lead.get("next_follow_up_at")


def test_repeated_coach_run_does_not_duplicate_follow_up(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    analysis = _analysis(
        suggested_follow_up_at=due, suggested_follow_up_reason="Call back Friday"
    )
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=analysis):
        sms_inbound.analyze_inbound_and_coach(u1, lead_id, None, "Call me Friday.")
        sms_inbound.analyze_inbound_and_coach(u1, lead_id, None, "Call me Friday.")
    assert len(_open_follow_ups(u1, lead_id)) == 1


def test_coach_appointment_requested_uses_high_priority(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    analysis = _analysis(
        suggested_follow_up_at=due,
        suggested_follow_up_reason="Confirm appointment",
        appointment_requested=True,
    )
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=analysis):
        sms_inbound.analyze_inbound_and_coach(u1, lead_id, None, "Can we meet Friday?")
    items = _open_follow_ups(u1, lead_id)
    assert len(items) == 1
    assert items[0]["priority"] == "high"


def test_coach_invalid_follow_up_at_is_ignored(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    analysis = _analysis(suggested_follow_up_at="not-a-real-date")
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=analysis):
        result = sms_inbound.analyze_inbound_and_coach(
            u1, lead_id, None, "Whenever works."
        )
    assert result["error"] is None
    assert _open_follow_ups(u1, lead_id) == []
