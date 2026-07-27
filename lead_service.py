"""Shared CRM lead upsert for AI SMS and AI Calling Assistant."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import crm_db
import db
from crm_constants import normalize_lead_status


VOICE_SOURCE = "AI Call Assistant"
SMS_SOURCE = "sms"


def normalize_phone_e164(phone_number: str) -> str:
    """Normalize a phone number to E.164 (+country…digits)."""
    raw = str(phone_number or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        return "+" + re.sub(r"\D", "", raw[1:])
    digits_only = re.sub(r"\D", "", raw)
    if len(digits_only) == 10:
        return "+1" + digits_only
    if len(digits_only) == 11 and digits_only.startswith("1"):
        return "+" + digits_only
    return "+" + digits_only if digits_only else ""


def _now():
    return datetime.now(timezone.utc).isoformat()


def upsert_crm_lead(
    user_id,
    phone_number,
    data=None,
    *,
    source=SMS_SOURCE,
    initial_status=None,
    touch_call=False,
    touch_sms=False,
    assigned_user_id=None,
):
    """Find or create a lead for (user_id, normalized phone). Never duplicates per tenant.

    Returns (lead_id, created: bool, lead: dict).
    """
    data = data or {}
    phone = normalize_phone_e164(phone_number)
    if not phone:
        raise ValueError("A valid phone number is required to upsert a lead.")

    now = _now()
    name = (data.get("lead_name") or data.get("name") or "").strip() or "Lead"
    lead_type = (data.get("lead_type") or "").strip() or None
    property_interest = (data.get("property_interest") or "").strip() or None
    notes_parts = [
        data.get("lead_context"),
        data.get("call_purpose"),
        data.get("notes"),
        data.get("desired_outcome"),
    ]
    notes = " | ".join(str(p).strip() for p in notes_parts if p and str(p).strip())[:1500] or None
    status = normalize_lead_status(initial_status) if initial_status else None
    assignee = assigned_user_id if assigned_user_id is not None else user_id

    existing = db.get_lead_by_phone(user_id, phone)
    if existing:
        lead_id = existing["id"]
        db.update_lead_contact_fields(
            lead_id,
            user_id,
            name=name if name != "Lead" else None,
            lead_type=lead_type,
            property_interest=property_interest,
            notes=notes,
            touch_call=touch_call,
            touch_sms=touch_sms,
            # Preserve existing source; never overwrite with a different channel source.
            bump_status_from_new_to=status if status == "attempting_contact" else None,
        )
        lead = db.get_lead(lead_id, user_id)
        return lead_id, False, lead

    create_status = status or "new"
    lead_id = db.create_lead_record(
        user_id=user_id,
        phone_number=phone,
        name=name,
        lead_type=lead_type,
        property_interest=property_interest,
        status=create_status,
        source=source,
        notes=notes,
        assigned_user_id=assignee,
        touch_call=touch_call,
        touch_sms=touch_sms,
    )
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "lead_created",
        f"Lead created from {source}",
        {"source": source, "phone_number": phone, "status": create_status},
        actor_user_id=user_id,
    )
    lead = db.get_lead(lead_id, user_id)
    return lead_id, True, lead


def link_voice_call_to_lead(call_id, lead_id, user_id):
    db.set_voice_call_lead_id(call_id, lead_id, user_id)


def record_voice_call_started(user_id, lead_id, call_id, *, phone_number, provider_call_id=None):
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "voice_call_started",
        "AI call started",
        {
            "voice_call_id": call_id,
            "provider_call_id": provider_call_id,
            "phone_number": phone_number,
            "status": "started",
        },
        actor_user_id=user_id,
    )
    db.touch_lead_call_timestamps(lead_id, user_id)


def apply_voice_call_webhook_to_lead(user_id, call_row, normalized):
    """Update linked lead from Vapi end-of-call data. Safe status rules only."""
    lead_id = call_row.get("lead_id")
    if not lead_id:
        return None

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None

    # Re-load call so recording fields persisted by the webhook update are included.
    fresh_call = db.get_voice_call(call_row.get("id"), user_id) or call_row

    status = (normalized.get("status") or fresh_call.get("status") or "").lower()
    outcome = normalized.get("outcome") or fresh_call.get("outcome")
    summary = normalized.get("summary") or fresh_call.get("summary")
    duration = (
        normalized.get("recording_duration_seconds")
        or normalized.get("duration")
        or fresh_call.get("recording_duration_seconds")
    )
    appointment_requested = bool(normalized.get("appointment_requested"))
    provider_call_id = normalized.get("provider_call_id") or fresh_call.get("provider_call_id")
    follow_up_at = normalized.get("follow_up_at")
    recording_status = (
        normalized.get("recording_status")
        or fresh_call.get("recording_status")
    )
    has_recording = db.voice_call_has_recording(fresh_call) or bool(
        normalized.get("recording_url") or normalized.get("stereo_recording_url")
    )
    if has_recording:
        recording_status = "available"

    event_type = "voice_call_completed" if status == "completed" else "voice_call_updated"
    activity_summary = (
        "Voice call completed"
        if status == "completed"
        else (
            f"AI call {status or 'updated'}"
            + (f": {(summary or outcome or '')[:120]}" if (summary or outcome) else "")
        )
    )

    payload = {
        "voice_call_id": fresh_call.get("id"),
        "provider_call_id": provider_call_id,
        "status": status or fresh_call.get("status"),
        "duration": duration,
        "recording_duration_seconds": duration,
        "summary": summary,
        "outcome": outcome,
        "appointment_requested": appointment_requested,
        "follow_up_at": follow_up_at,
        "has_recording": has_recording,
        "recording_status": recording_status,
        "has_transcript": bool(normalized.get("transcript") or fresh_call.get("transcript")),
        "completed_at": fresh_call.get("completed_at"),
        "recommended_next_action": normalized.get("recommended_next_action")
        or ("Schedule follow-up appointment" if appointment_requested else None),
    }

    # Idempotent: Vapi may retry webhooks — update existing activity instead of duplicating.
    existing = crm_db.find_lead_activity_for_voice_call(
        user_id, lead_id, fresh_call.get("id"), event_type
    )
    if existing:
        crm_db.update_lead_activity(
            user_id, existing["id"], summary=activity_summary, payload=payload
        )
    else:
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            event_type,
            activity_summary,
            payload,
            actor_user_id=user_id,
        )

    db.touch_lead_call_timestamps(lead_id, user_id)
    if summary or outcome:
        db.merge_lead_call_outcome_notes(
            lead_id,
            user_id,
            summary=summary,
            outcome=outcome,
            next_action=payload.get("recommended_next_action"),
            follow_up_at=follow_up_at,
        )

    current = normalize_lead_status(lead.get("status"))
    suggested_status = None

    # Safe deterministic automation only.
    if appointment_requested and current not in {"do_not_contact", "closed_won", "closed_lost"}:
        if current in {"new", "attempting_contact", "contacted", "qualified", "nurture"}:
            crm_db.set_lead_status(
                user_id, lead_id, "appointment_scheduled", from_automation=True
            )
        else:
            suggested_status = "appointment_scheduled"
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            "appointment_requested",
            priority="high",
            source_ref_type="voice_call",
            source_ref_id=call_row.get("id"),
            reason_text="AI call indicated an appointment should be scheduled.",
        )
    elif status == "completed" and current in {"new", "attempting_contact"}:
        crm_db.set_lead_status(user_id, lead_id, "contacted", from_automation=True)
    elif status == "failed" and current in {"new", "attempting_contact"}:
        # Stay in attempting_contact; surface for follow-up.
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            "call_failed",
            priority="normal",
            source_ref_type="voice_call",
            source_ref_id=call_row.get("id"),
            reason_text="AI call failed or did not connect — follow up manually.",
        )
        suggested_status = None
    elif summary and current not in {"do_not_contact", "closed_won", "closed_lost"}:
        # Non-deterministic nuance → suggest only.
        if "not interested" in (summary or "").lower():
            suggested_status = "closed_lost"
        elif "nurture" in (summary or "").lower() or "later" in (summary or "").lower():
            suggested_status = "nurture"

    if suggested_status and suggested_status != normalize_lead_status(
        db.get_lead(lead_id, user_id).get("status")
    ):
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "status_suggestion",
            f"Suggested status: {suggested_status}",
            {"suggested_status": suggested_status, "source": "voice_webhook"},
            actor_user_id=user_id,
        )
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            "review_call_outcome",
            priority="normal",
            source_ref_type="voice_call",
            source_ref_id=call_row.get("id"),
            reason_text=f"Review AI call outcome; suggested status: {suggested_status}.",
        )

    return db.get_lead(lead_id, user_id)
