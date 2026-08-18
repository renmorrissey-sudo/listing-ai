"""AI SMS Agent: automated conversational replies to inbound lead SMS.

Flow (invoked from the Telnyx message.received webhook, off the request thread):
  1. Run the existing Claude coach analysis once (stores insight + draft).
  2. Enforce consent / opt-out / suppression / provider gates.
  3. Claim the single reply slot for the inbound message (durable unique index).
  4. Send exactly one reply through the same Telnyx number that received the SMS.
  5. Persist the outbound message + Telnyx message id and touch CRM activity.

On AI failure the inbound message is retained, the conversation is flagged for
manual attention, and nothing is retried automatically (no duplicate sends).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import config
import crm_db
import db
import sms_coach
import tenant_sms_db as tdb

logger = logging.getLogger(__name__)

# Escalation topics never get an automated reply; the agent follows up personally.
_NO_AUTO_REPLY_TOPICS = {"legal", "financing", "fair_housing", "complaint"}


def auto_reply_configured() -> bool:
    return bool(config.SMS_AI_AUTO_REPLY_ENABLED) and sms_coach.is_configured()


def schedule_inbound_ai(app, user_id, lead_id, inbound_id, body, receiving_number):
    """Run analysis + auto-reply in a background thread so the webhook acks fast."""

    def run():
        try:
            if app is not None:
                with app.app_context():
                    process_inbound_ai(user_id, lead_id, inbound_id, body, receiving_number)
            else:
                process_inbound_ai(user_id, lead_id, inbound_id, body, receiving_number)
        except Exception:
            logger.exception(
                "SMS_PROCESSING_ERROR stage=ai_thread inbound_id=%s lead_id=%s tenant=%s",
                inbound_id,
                lead_id,
                user_id,
            )

    threading.Thread(target=run, name=f"sms-ai-{inbound_id}", daemon=True).start()


def process_inbound_ai(user_id, lead_id, inbound_id, inbound_body, receiving_number):
    """
    Analyze the inbound SMS and send at most ONE automated reply.
    Returns a result dict for tests/observability.
    """
    from sms_inbound import analyze_inbound_and_coach

    logger.info(
        "SMS_AI_STARTED inbound_id=%s lead_id=%s tenant=%s", inbound_id, lead_id, user_id
    )
    coach = analyze_inbound_and_coach(user_id, lead_id, inbound_id, inbound_body) or {}
    analysis = coach.get("analysis")
    if coach.get("error") == "coach_failed":
        # Inbound message is already persisted; flag for manual follow-up.
        logger.error(
            "SMS_PROCESSING_ERROR stage=ai_generation inbound_id=%s lead_id=%s tenant=%s",
            inbound_id,
            lead_id,
            user_id,
        )
        _needs_attention(user_id, lead_id, inbound_id, "ai_reply_failed")
        return {"replied": False, "reason": "ai_generation_failed"}
    if not analysis:
        return {"replied": False, "reason": coach.get("error") or "no_analysis"}

    logger.info(
        "SMS_AI_COMPLETED inbound_id=%s lead_id=%s tenant=%s manual_review=%s",
        inbound_id,
        lead_id,
        user_id,
        bool(analysis.get("requires_manual_review")),
    )

    if not auto_reply_configured():
        return {"replied": False, "reason": "auto_reply_disabled"}

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return {"replied": False, "reason": "lead_missing"}

    allowed, reason = _auto_reply_allowed(user_id, lead, analysis)
    if not allowed:
        logger.info(
            "SMS_AI_REPLY_SKIPPED inbound_id=%s lead_id=%s tenant=%s reason=%s",
            inbound_id,
            lead_id,
            user_id,
            reason,
        )
        return {"replied": False, "reason": reason}

    draft = str(analysis.get("draft_reply") or "").strip()[:480]
    if not draft:
        return {"replied": False, "reason": "empty_draft"}

    to_number = lead.get("phone_number")
    # Claim the reply slot BEFORE sending: the unique index on reply_to_message_id
    # guarantees a duplicate webhook or concurrent worker cannot send twice.
    message_id = db.create_ai_reply_message(
        user_id,
        lead_id,
        inbound_id,
        phone_number=to_number,
        message_body=draft,
        provider=config.SMS_PROVIDER,
        lead_name=lead.get("name"),
    )
    if message_id is None:
        logger.info(
            "SMS_AI_REPLY_DUPLICATE inbound_id=%s lead_id=%s tenant=%s",
            inbound_id,
            lead_id,
            user_id,
        )
        existing = db.get_sent_ai_outbound_for_inbound(inbound_id)
        if existing:
            db.consume_pending_suggestion_after_auto_reply(
                user_id,
                coach.get("insight_id"),
                coach.get("suggested_id"),
            )
        return {"replied": False, "reason": "already_replied"}

    from sms_quiet_hours import in_quiet_hours, next_permitted_send_at

    if in_quiet_hours(user_id, phone=to_number, lead=lead):
        send_at = next_permitted_send_at(user_id, phone=to_number, lead=lead)
        db.schedule_sms_message(message_id, send_at.isoformat())
        db.consume_pending_suggestion_after_auto_reply(
            user_id,
            coach.get("insight_id"),
            coach.get("suggested_id"),
        )
        logger.info(
            "SMS_AI_REPLY_SCHEDULED inbound_id=%s message_id=%s scheduled_for=%s tenant=%s",
            inbound_id,
            message_id,
            send_at.isoformat(),
            user_id,
        )
        return {
            "replied": True,
            "scheduled": True,
            "message_id": message_id,
            "scheduled_for": send_at.isoformat(),
        }

    from sms_provider import sms_status_callback_url
    from sms_providers import SmsProviderError, get_sms_provider

    provider = get_sms_provider()
    from_number = receiving_number or None
    logger.info(
        "SMS_OUTBOUND_REQUEST message_id=%s inbound_id=%s lead_id=%s tenant=%s ai=1",
        message_id,
        inbound_id,
        lead_id,
        user_id,
    )
    try:
        result = provider.send_sms(
            to_number,
            draft,
            status_callback=sms_status_callback_url(),
            from_number=from_number,
        )
    except SmsProviderError as exc:
        db.update_sms_message_send_result(
            message_id, status="failed", error_message=str(exc)[:500]
        )
        logger.error(
            "SMS_PROCESSING_ERROR stage=ai_outbound message_id=%s lead_id=%s tenant=%s",
            message_id,
            lead_id,
            user_id,
        )
        _needs_attention(user_id, lead_id, inbound_id, "ai_reply_failed")
        return {"replied": False, "reason": "send_failed", "message_id": message_id}
    except Exception:
        db.update_sms_message_send_result(
            message_id,
            status="failed",
            error_message="AI reply could not be sent due to an internal error.",
        )
        logger.exception(
            "SMS_PROCESSING_ERROR stage=ai_outbound message_id=%s lead_id=%s tenant=%s",
            message_id,
            lead_id,
            user_id,
        )
        _needs_attention(user_id, lead_id, inbound_id, "ai_reply_failed")
        return {"replied": False, "reason": "send_failed", "message_id": message_id}

    provider_message_id = result.get("provider_message_id")
    db.update_sms_message_send_result(
        message_id,
        provider_message_id=str(provider_message_id) if provider_message_id else None,
        status=result.get("status") or "queued",
    )
    db.touch_lead_outbound(lead_id, user_id)
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "sms_ai_reply",
        "AI SMS Agent replied automatically",
        {
            "message_id": message_id,
            "reply_to_message_id": inbound_id,
            "provider_message_id": provider_message_id,
        },
    )
    tdb.append_sms_audit(
        user_id,
        "ai_reply_sent",
        lead_id=lead_id,
        metadata={
            "message_id": message_id,
            "reply_to_message_id": inbound_id,
            "provider_message_id": provider_message_id,
        },
    )
    logger.info(
        "SMS_OUTBOUND_ACCEPTED message_id=%s telnyx_message_id=%s lead_id=%s tenant=%s ai=1",
        message_id,
        provider_message_id,
        lead_id,
        user_id,
    )
    db.consume_pending_suggestion_after_auto_reply(
        user_id,
        coach.get("insight_id"),
        coach.get("suggested_id"),
    )
    return {
        "replied": True,
        "message_id": message_id,
        "provider_message_id": provider_message_id,
    }


def _auto_reply_allowed(user_id, lead, analysis):
    """
    Consent, compliance, and safety gates for automated replies.
    Mirrors can_send_sms consent semantics (the reply answers a consumer-initiated
    inbound message, so no per-message attestation applies) plus AI-specific guards.
    """
    from sms_authorization import (
        check_telnyx_toll_free_send_allowed,
        check_telnyx_trial_destination,
    )
    from sms_consent import outbound_sms_blocked_for_phone

    phone = lead.get("phone_number") or ""
    if (lead.get("opt_out_status") or "active") == "opted_out":
        return False, "opted_out"
    consent_status = (lead.get("sms_consent_status") or "").lower()
    if consent_status in {"opted_out", "revoked", "not_permitted", "suppressed", "invalid_number"}:
        return False, "consent_blocked"
    if tdb.is_suppressed(user_id, phone):
        return False, "suppressed"
    if outbound_sms_blocked_for_phone(phone):
        return False, "inquiry_blocked"

    topics = set(analysis.get("escalation_topics") or [])
    if analysis.get("sensitive_topic") and topics & _NO_AUTO_REPLY_TOPICS:
        return False, "escalation_topic"

    toll_ok, _err = check_telnyx_toll_free_send_allowed()
    if not toll_ok:
        return False, "toll_free_not_verified"
    trial_ok, _err = check_telnyx_trial_destination(phone)
    if not trial_ok:
        return False, "trial_destination_blocked"

    since = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    if (
        db.count_ai_replies_to_contact_since(user_id, phone, since)
        >= config.SMS_AI_MAX_REPLIES_PER_CONTACT_PER_DAY
    ):
        return False, "reply_rate_limited"
    return True, None


def _needs_attention(user_id, lead_id, inbound_id, reason):
    try:
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            reason,
            priority="high",
            source_ref_type="sms",
            source_ref_id=inbound_id,
        )
    except Exception:
        logger.exception(
            "Failed to flag needs attention lead_id=%s tenant=%s", lead_id, user_id
        )
