"""Shared CRM lead upsert for AI SMS and AI Calling Assistant."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

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


def format_phone_display(phone_number: str) -> str:
    """Human-readable US number, e.g. +1 303-870-3107."""
    normalized = normalize_phone_e164(phone_number)
    digits = re.sub(r"\D", "", normalized)
    if len(digits) == 11 and digits.startswith("1"):
        national = digits[1:]
        return f"+1 {national[0:3]}-{national[3:6]}-{national[6:]}"
    return normalized or str(phone_number or "").strip()


def is_placeholder_lead_name(name) -> bool:
    return (name or "").strip() in {"", "Lead", "External Lead"}


def duplicate_phone_message(lead) -> str:
    name = (lead or {}).get("name") or "Lead"
    phone = format_phone_display((lead or {}).get("phone_number") or "")
    return f"A lead with this phone number already exists: {name} ({phone})."


def _fill_if_blank(existing_value, incoming, *, placeholder_ok=False):
    """Return incoming only when the stored field is empty (never overwrite identity)."""
    current = (existing_value or "").strip()
    if placeholder_ok and is_placeholder_lead_name(current):
        incoming_s = (incoming or "").strip()
        return incoming if incoming_s and not is_placeholder_lead_name(incoming_s) else None
    if current:
        return None
    return incoming


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

    # Formatting-tolerant match so "(303) 555-1212" and "+13035551212" never
    # create duplicate leads for the same contact.
    existing = db.find_lead_by_phone_normalized(user_id, phone)
    if existing:
        lead_id = existing["id"]
        # Match by phone for conversation continuity, but never silently replace
        # the existing lead's name or other CRM identity fields.
        db.update_lead_contact_fields(
            lead_id,
            user_id,
            name=_fill_if_blank(existing.get("name"), name, placeholder_ok=True),
            lead_type=_fill_if_blank(existing.get("lead_type"), lead_type),
            property_interest=_fill_if_blank(
                existing.get("property_interest"), property_interest
            ),
            notes=notes,
            touch_call=touch_call,
            touch_sms=touch_sms,
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


def earliest_sms_lead_name(user_id, lead_id):
    """Oldest stored SMS lead_name for this CRM row, if any. Never invents a name."""
    return db.earliest_sms_lead_name(user_id, lead_id)


def restore_lead_name_from_sms_history(user_id, lead_id):
    """
    Restore leads.name from the earliest SMS row for this lead when it differs.

    Only updates the name column. Does not delete, reassign, or rewrite SMS rows.
    """
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    historical = earliest_sms_lead_name(user_id, lead_id)
    if not historical:
        return lead, "No historical SMS name is available to restore."
    current = (lead.get("name") or "").strip()
    if current == historical:
        return lead, None
    db.update_lead_contact_fields(lead_id, user_id, name=historical)
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "lead_name_restored",
        f"Lead name restored to {historical} from SMS history",
        {"previous_name": current, "restored_name": historical, "source": "sms_history"},
        actor_user_id=user_id,
    )
    return db.get_lead(lead_id, user_id), None


def link_voice_call_to_lead(call_id, lead_id, user_id):
    db.set_voice_call_lead_id(call_id, lead_id, user_id)


def record_voice_call_started(user_id, lead_id, call_id, *, phone_number, provider_call_id=None):
    """Touch CRM timestamps when a call is placed.

    Intentionally does not write a timeline activity — queued/started dialer
    states are noise for agents and are filtered from the Activity Timeline.
    """
    db.touch_lead_call_timestamps(lead_id, user_id)


def _default_follow_up_due(*, days=1, hour_utc=15):
    due = datetime.now(timezone.utc) + timedelta(days=days)
    due = due.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return due.isoformat()


def recommend_voice_next_action(*, appointment_requested, meaningful_status, summary, recommended_next_action=None):
    explicit = str(recommended_next_action or "").strip()
    if explicit:
        return explicit[:500]
    if appointment_requested:
        return "Confirm appointment details and send a calendar invite"
    status = (meaningful_status or "").lower()
    if status in {"failed", "unanswered", "declined", "cancelled"}:
        return "Retry outreach — prior AI call did not connect"
    if status == "completed":
        if summary:
            return f"Follow up on call: {str(summary).strip()[:160]}"
        return "Review call summary and follow up with the lead"
    return None


def ensure_lead_follow_through(
    user_id,
    lead_id,
    *,
    appointment_requested=False,
    meaningful_status=None,
    summary=None,
    recommended_next_action=None,
    follow_up_at=None,
):
    """Populate Next Action and Follow-up after a meaningful voice outcome."""
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None

    next_action = recommend_voice_next_action(
        appointment_requested=appointment_requested
        or normalize_lead_status(lead.get("status")) == "appointment_scheduled",
        meaningful_status=meaningful_status,
        summary=summary,
        recommended_next_action=recommended_next_action,
    )
    if next_action:
        db.merge_lead_call_outcome_notes(
            lead_id,
            user_id,
            next_action=next_action,
            follow_up_at=follow_up_at,
        )

    # Schedule a follow-up date when missing (idempotent on webhook retry).
    lead = db.get_lead(lead_id, user_id) or lead
    if follow_up_at and not lead.get("next_follow_up_at"):
        reason = (
            "Confirm appointment from AI call"
            if appointment_requested
            else (str(summary or "Follow up after AI call")[:200])
        )
        crm_db.set_lead_follow_up(
            user_id,
            lead_id,
            follow_up_at,
            reason,
            priority="high" if appointment_requested else "normal",
        )
    elif not lead.get("next_follow_up_at") and (
        appointment_requested
        or meaningful_status in {
            "completed",
            "failed",
            "unanswered",
            "declined",
            "cancelled",
        }
    ):
        days = 1 if appointment_requested or meaningful_status in {
            "failed",
            "unanswered",
            "declined",
            "cancelled",
        } else 2
        reason = (
            "Confirm appointment from AI call"
            if appointment_requested
            else (
                "Retry outreach after missed AI call"
                if meaningful_status in {"failed", "unanswered", "declined", "cancelled"}
                else "Follow up after AI call"
            )
        )
        crm_db.set_lead_follow_up(
            user_id,
            lead_id,
            _default_follow_up_due(days=days),
            reason,
            priority="high" if appointment_requested else "normal",
        )
    return db.get_lead(lead_id, user_id)


def _normalize_token(value):
    return str(value or "").strip().lower().replace("_", "-")


def classify_voice_timeline_event(normalized):
    """Map a Vapi webhook into a meaningful timeline event, or None if transient."""
    webhook_status = _normalize_token(normalized.get("status"))
    lifecycle = _normalize_token(normalized.get("lifecycle_status"))
    ended_reason = _normalize_token(normalized.get("ended_reason"))
    outcome = _normalize_token(normalized.get("outcome"))
    event_type = _normalize_token(normalized.get("event_type"))

    has_artifacts = bool(
        normalized.get("summary")
        or normalized.get("transcript")
        or normalized.get("recording_url")
        or normalized.get("stereo_recording_url")
    )

    if webhook_status == "completed" or event_type in {
        "end-of-call-report",
        "call-ended",
        "call-analyzed",
    }:
        label = "Voice call completed"
        if ended_reason in {"customer-ended-call", "customer-ended", "hangup"}:
            label = "Call ended by customer"
        return {
            "event_type": "voice_call_completed",
            "meaningful_status": "completed",
            "summary": label,
            "emit": True,
        }

    terminal = ended_reason or outcome
    failure_markers = (
        "failed",
        "error",
        "busy",
        "no-answer",
        "noanswer",
        "unanswered",
        "voicemail",
        "sip-error",
        "twilio-failed",
        "vonage-failed",
    )
    decline_markers = ("declined", "rejected")
    cancel_markers = ("cancel", "cancelled", "canceled")
    unanswered_markers = ("no-answer", "noanswer", "unanswered", "customer-did-not-answer")

    if any(marker in terminal for marker in unanswered_markers):
        return {
            "event_type": "voice_call_unanswered",
            "meaningful_status": "unanswered",
            "summary": "Call unanswered",
            "emit": True,
        }
    if any(marker in terminal for marker in decline_markers):
        return {
            "event_type": "voice_call_failed",
            "meaningful_status": "declined",
            "summary": "Call declined",
            "emit": True,
        }
    if any(marker in terminal for marker in cancel_markers):
        return {
            "event_type": "voice_call_cancelled",
            "meaningful_status": "cancelled",
            "summary": "Call cancelled",
            "emit": True,
        }
    if any(marker in terminal for marker in failure_markers) or lifecycle == "failed":
        return {
            "event_type": "voice_call_failed",
            "meaningful_status": "failed",
            "summary": "Call failed",
            "emit": True,
        }

    if lifecycle in {"in-progress", "forwarding", "connected"}:
        return {
            "event_type": "voice_call_connected",
            "meaningful_status": "connected",
            "summary": "Call connected",
            "emit": True,
        }

    if lifecycle in crm_db.VOICE_TRANSIENT_STATUSES or terminal in crm_db.VOICE_TRANSIENT_STATUSES:
        return {
            "event_type": None,
            "meaningful_status": lifecycle or terminal or "queued",
            "summary": None,
            "emit": False,
        }

    # Status-update noise with no terminal reason and no artifacts.
    if event_type in {"status-update", "speech-update", "transcript", "hang"} and not has_artifacts:
        return {
            "event_type": None,
            "meaningful_status": lifecycle or terminal or event_type,
            "summary": None,
            "emit": False,
        }

    if has_artifacts:
        return {
            "event_type": "voice_call_completed",
            "meaningful_status": "completed",
            "summary": "Voice call completed",
            "emit": True,
        }

    return {
        "event_type": None,
        "meaningful_status": lifecycle or terminal or "ignored",
        "summary": None,
        "emit": False,
    }


def _upsert_voice_call_activity(user_id, lead_id, event_type, summary, payload):
    """Idempotent upsert keyed by tenant + lead + voice_call_id (+ provider id)."""
    voice_call_id = payload.get("voice_call_id")
    existing = crm_db.find_lead_activity_for_voice_call(
        user_id, lead_id, voice_call_id, event_type=None
    )
    if existing:
        existing_payload = crm_db.parse_activity_payload(existing)
        # Never downgrade a completed/failed row back to connected.
        existing_type = existing.get("event_type")
        if existing_type == "voice_call_completed" and event_type != "voice_call_completed":
            merged = dict(existing_payload)
            merged.update({k: v for k, v in payload.items() if v not in (None, "", [])})
            crm_db.update_lead_activity(
                user_id,
                existing["id"],
                summary=existing.get("summary") or summary,
                payload=merged,
                event_type="voice_call_completed",
            )
            return existing["id"]
        merged = dict(existing_payload)
        merged.update(payload)
        crm_db.update_lead_activity(
            user_id,
            existing["id"],
            summary=summary,
            payload=merged,
            event_type=event_type,
        )
        return existing["id"]
    return crm_db.add_lead_activity(
        lead_id,
        user_id,
        event_type,
        summary,
        payload,
        actor_user_id=user_id,
    )


def apply_voice_call_webhook_to_lead(user_id, call_row, normalized):
    """Update linked lead from Vapi webhooks. Idempotent; skips transient dialer noise."""
    lead_id = call_row.get("lead_id")
    if not lead_id:
        return None

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None

    # Re-load call so recording fields persisted by the webhook update are included.
    fresh_call = db.get_voice_call(call_row.get("id"), user_id) or call_row
    classified = classify_voice_timeline_event(normalized)

    outcome = (
        normalized.get("ended_reason")
        or normalized.get("outcome")
        or fresh_call.get("outcome")
    )
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
    has_transcript = bool(normalized.get("transcript") or fresh_call.get("transcript"))
    meaningful_status = classified.get("meaningful_status")
    webhook_completed = (normalized.get("status") or "").lower() == "completed"

    payload = {
        "voice_call_id": fresh_call.get("id"),
        "provider_call_id": provider_call_id,
        "dedupe_key": (
            f"{user_id}:{lead_id}:{provider_call_id or fresh_call.get('id')}:"
            f"{classified.get('event_type') or meaningful_status}"
        ),
        "status": meaningful_status,
        "lifecycle_status": normalized.get("lifecycle_status"),
        "ended_reason": normalized.get("ended_reason"),
        "meaningful_status": meaningful_status,
        "duration": duration,
        "recording_duration_seconds": duration,
        "summary": summary,
        "outcome": outcome,
        "appointment_requested": appointment_requested,
        "follow_up_at": follow_up_at,
        "has_recording": has_recording,
        "recording_status": recording_status,
        "has_transcript": has_transcript,
        "completed_at": fresh_call.get("completed_at"),
        "recommended_next_action": recommend_voice_next_action(
            appointment_requested=appointment_requested,
            meaningful_status=meaningful_status,
            summary=summary,
            recommended_next_action=normalized.get("recommended_next_action"),
        ),
    }

    emitted = False
    if classified.get("emit") and classified.get("event_type"):
        activity_summary = classified["summary"]
        if webhook_completed:
            activity_summary = "Voice call completed"
        _upsert_voice_call_activity(
            user_id,
            lead_id,
            classified["event_type"],
            activity_summary,
            payload,
        )
        db.touch_lead_call_timestamps(lead_id, user_id)
        if summary or outcome:
            db.merge_lead_call_outcome_notes(
                lead_id,
                user_id,
                summary=summary,
                outcome=outcome,
            )
        emitted = True
    elif has_recording or has_transcript or summary:
        # Artifacts arrived without a classified event — still consolidate onto completed.
        _upsert_voice_call_activity(
            user_id,
            lead_id,
            "voice_call_completed",
            "Voice call completed",
            payload,
        )
        db.touch_lead_call_timestamps(lead_id, user_id)
        if summary or outcome:
            db.merge_lead_call_outcome_notes(
                lead_id,
                user_id,
                summary=summary,
                outcome=outcome,
            )
        emitted = True
        meaningful_status = meaningful_status or "completed"

    # Lead status automation only for meaningful terminal / completed states.
    current = normalize_lead_status(lead.get("status"))
    suggested_status = None
    status_for_rules = meaningful_status if emitted else None

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
    elif status_for_rules == "completed" and current in {"new", "attempting_contact"}:
        crm_db.set_lead_status(user_id, lead_id, "contacted", from_automation=True)
    elif status_for_rules in {"failed", "unanswered", "declined", "cancelled"} and current in {
        "new",
        "attempting_contact",
    }:
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            "call_failed",
            priority="normal",
            source_ref_type="voice_call",
            source_ref_id=call_row.get("id"),
            reason_text="AI call failed or did not connect — follow up manually.",
        )
    elif summary and webhook_completed and current not in {
        "do_not_contact",
        "closed_won",
        "closed_lost",
    }:
        if "not interested" in (summary or "").lower():
            suggested_status = "closed_lost"
        elif "nurture" in (summary or "").lower() or "later" in (summary or "").lower():
            suggested_status = "nurture"

    if emitted or appointment_requested:
        ensure_lead_follow_through(
            user_id,
            lead_id,
            appointment_requested=appointment_requested,
            meaningful_status=meaningful_status or status_for_rules,
            summary=summary,
            recommended_next_action=payload.get("recommended_next_action"),
            follow_up_at=follow_up_at,
        )

    if suggested_status and suggested_status != normalize_lead_status(
        db.get_lead(lead_id, user_id).get("status")
    ):
        existing_suggestion = None
        for activity in crm_db.list_lead_activities(user_id, lead_id, limit=50, for_timeline=False):
            if activity.get("event_type") != "status_suggestion":
                continue
            payload_row = crm_db.parse_activity_payload(activity)
            if payload_row.get("source") == "voice_webhook" and payload_row.get(
                "voice_call_id"
            ) == fresh_call.get("id"):
                existing_suggestion = activity
                break
        suggestion_payload = {
            "suggested_status": suggested_status,
            "source": "voice_webhook",
            "voice_call_id": fresh_call.get("id"),
            "provider_call_id": provider_call_id,
        }
        if existing_suggestion:
            crm_db.update_lead_activity(
                user_id,
                existing_suggestion["id"],
                summary=f"Suggested status: {suggested_status}",
                payload=suggestion_payload,
            )
        else:
            crm_db.add_lead_activity(
                lead_id,
                user_id,
                "status_suggestion",
                f"Suggested status: {suggested_status}",
                suggestion_payload,
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
