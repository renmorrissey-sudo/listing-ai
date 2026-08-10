"""Telnyx Messaging Profile webhook — Ed25519 verified, fast ack, async AI reply.

Event routing (Telnyx Messaging API V2 envelopes: {"data": {"event_type", "id",
"payload": {...}}}):
  message.received        → persist inbound, update CRM, schedule AI SMS Agent reply
  message.sent            → outbound delivery-status update only (never AI)
  message.finalized       → final delivery-status update only (never AI)
  message.delivery_failed → failure status update + needs-attention (never AI)

Idempotency: data.id is claimed in sms_webhook_events (UNIQUE constraint) before
processing; payload.id (the Telnyx message id) dedupes the message row itself; and
the AI reply slot is unique per inbound message. A retried or duplicated webhook can
therefore never double-store a message or double-send an AI reply.
"""

from __future__ import annotations

import logging

import crm_db
import db
import external_leads_db as xdb
import tenant_sms_db as tdb
from sms_providers.telnyx import TelnyxSMSProvider
from sms_validation import validate_e164_phone

logger = logging.getLogger(__name__)


def _normalize_phone(raw):
    cleaned, err = validate_e164_phone(raw if str(raw or "").startswith("+") else f"+{raw}")
    if cleaned:
        return cleaned
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) == 10:
        cleaned, _ = validate_e164_phone(f"+1{digits}")
        return cleaned
    if len(digits) == 11 and digits.startswith("1"):
        cleaned, _ = validate_e164_phone(f"+{digits}")
        return cleaned
    return None


def _event_type(payload):
    data = (payload or {}).get("data") or {}
    return (data.get("event_type") or "").strip()


def handle_messaging_webhook(payload: dict, *, app=None):
    """
    Process Telnyx Messaging API V2 webhook payload.
    Returns (result_dict, http_status). Raises on unexpected processing errors after
    marking the webhook event 'failed' so a Telnyx retry can reprocess it.
    """
    event_type = _event_type(payload)
    data = (payload or {}).get("data") or {}
    event_id = data.get("id")
    telnyx_message_id = (data.get("payload") or {}).get("id")

    claim = tdb.claim_webhook_event(
        provider="telnyx",
        provider_event_id=str(event_id) if event_id else None,
        event_type=event_type or "unknown",
        provider_message_id=str(telnyx_message_id) if telnyx_message_id else None,
        safe_metadata={"event_type": event_type},
    )
    if claim == "duplicate":
        logger.info(
            "TELNYX_EVENT_DUPLICATE webhookEventId=%s event_type=%s",
            event_id,
            event_type,
        )
        return {"ok": True, "duplicate": True}, 200

    try:
        if event_type == "message.received":
            result, status = _handle_received(payload, app=app)
        elif event_type in {
            "message.sent",
            "message.finalized",
            "message.delivery_failed",
        }:
            result, status = _handle_delivery(payload)
        else:
            # Unknown — ack so Telnyx does not retry forever
            logger.info(
                "Telnyx webhook ignored event_type=%s webhookEventId=%s",
                event_type or "none",
                event_id,
            )
            result, status = {"ok": True, "ignored": True}, 200
    except Exception:
        # Leave the event reprocessable: Telnyx retries after our 5xx.
        tdb.mark_webhook_processed("telnyx", str(event_id or ""), "failed")
        logger.exception(
            "SMS_PROCESSING_ERROR stage=webhook webhookEventId=%s event_type=%s",
            event_id,
            event_type,
        )
        raise

    if event_id:
        tdb.mark_webhook_processed("telnyx", str(event_id), "processed")
    return result, status


def _resolve_tenant_sender(account_phone, contact_phone):
    """
    Identify the tenant that owns this inbound SMS.

    Deterministic rules, in order:
      1. A tenant sender row matching the receiving number whose tenant already
         has a lead/conversation with the sending phone number.
      2. When the receiving number is the shared platform TELNYX_PHONE_NUMBER
         (sender rows are unique per number, but every subscriber sends from it),
         the tenant that owns the most recently active matching lead.
      3. The sender row that owns the receiving number.
      4. For the platform number only: any active Telnyx sender.
    """
    import config

    candidates = tdb.list_senders_by_number(account_phone)
    if contact_phone:
        for sender in candidates:
            if db.find_lead_by_phone_normalized(sender["user_id"], contact_phone):
                return sender

    platform = (config.TELNYX_PHONE_NUMBER or "").strip()
    is_platform = bool(platform) and _normalize_phone(account_phone) == _normalize_phone(platform)
    if is_platform and contact_phone:
        owner_id = db.find_lead_owner_by_phone(contact_phone)
        if owner_id:
            return {
                "user_id": owner_id,
                "sender_number": platform,
                "sms_provider": "telnyx",
                "platform_sender": True,
            }
    if candidates:
        return candidates[0]
    if is_platform:
        return tdb.get_any_telnyx_sender()
    return None


def _handle_received(payload, *, app=None):
    from sms_inbound import classify_compliance_keyword

    provider = TelnyxSMSProvider()
    event = provider.normalize_inbound_webhook(payload)
    event_id = event.get("event_id")
    pmid = event.get("provider_message_id")
    logger.info(
        "TELNYX_INBOUND_MESSAGE webhookEventId=%s telnyxMessageId=%s", event_id, pmid
    )
    if pmid and db.get_sms_message_by_provider_id(str(pmid)):
        logger.info(
            "TELNYX_EVENT_DUPLICATE telnyxMessageId=%s (message already stored)", pmid
        )
        return {"ok": True, "duplicate": True}, 200

    account_phone = event.get("account_phone")
    contact = _normalize_phone(event.get("contact_phone"))
    if not contact:
        logger.warning(
            "SMS_PROCESSING_ERROR stage=inbound_parse webhookEventId=%s error=invalid_contact",
            event_id,
        )
        return {"ok": False, "error": "invalid_contact"}, 400

    sender = _resolve_tenant_sender(account_phone, contact)
    if not sender:
        # Deterministic + safe: ack so Telnyx stops retrying a number we do not own.
        logger.warning(
            "SMS_PROCESSING_ERROR stage=tenant_match webhookEventId=%s error=unknown_destination",
            event_id,
        )
        return {"ok": True, "ignored": "unknown_destination"}, 200

    user_id = sender["user_id"]

    lead = db.find_lead_by_phone_normalized(user_id, contact)
    if not lead:
        from lead_service import upsert_crm_lead

        lead_id, _, lead = upsert_crm_lead(
            user_id,
            contact,
            {"name": "SMS Contact", "phone_number": contact},
            source="sms_inbound",
            touch_sms=True,
            assigned_user_id=user_id,
        )
        logger.info(
            "SMS_LEAD_MATCHED leadId=%s tenant=%s created=1 webhookEventId=%s",
            lead_id,
            user_id,
            event_id,
        )
    else:
        lead_id = lead["id"]
        logger.info(
            "SMS_LEAD_MATCHED leadId=%s tenant=%s created=0 webhookEventId=%s",
            lead_id,
            user_id,
            event_id,
        )
    logger.info(
        "SMS_CONVERSATION_MATCHED leadId=%s tenant=%s telnyxMessageId=%s",
        lead_id,
        user_id,
        pmid,
    )

    text = (event.get("text") or "").strip()[:1500]
    if not text and event.get("media_items"):
        text = "[media message]"
    keyword = classify_compliance_keyword(text)

    message_id = db.create_sms_message(
        user_id=user_id,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": (lead or {}).get("name") if lead else "SMS Contact",
            "phone_number": contact,
            "message_body": text,
        },
        status="received",
        lead_id=lead_id,
        direction="inbound",
        consent_status=(lead or {}).get("consent_status") or "unknown",
        opt_out_status="opted_out" if keyword == "opt_out" else ((lead or {}).get("opt_out_status") or "active"),
    )
    if pmid:
        db.update_sms_message_send_result(message_id, provider_message_id=str(pmid), status="received")
    logger.info(
        "SMS_INBOUND_PERSISTED messageId=%s telnyxMessageId=%s leadId=%s tenant=%s",
        message_id,
        pmid,
        lead_id,
        user_id,
    )

    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "sms_inbound",
        "Inbound SMS received",
        {"message_id": message_id, "provider": "telnyx"},
    )
    crm_db.upsert_needs_attention(
        user_id,
        lead_id,
        "unreviewed_inbound",
        priority="high",
        source_ref_type="sms",
        source_ref_id=message_id,
    )
    _touch_inbound_lead_state(user_id, lead_id, lead, keyword)

    if keyword == "opt_out":
        _apply_opt_out(user_id, lead_id, contact, source="telnyx_inbound")
    elif keyword == "opt_in":
        # Do not silently clear suppression; only clear legacy opt-out flag if present
        try:
            db.clear_lead_sms_opt_out(lead_id, user_id)
        except Exception:
            pass
        tdb.append_sms_audit(
            user_id,
            "opt_in_received",
            lead_id=lead_id,
            metadata={"note": "START received; suppression not auto-cleared"},
        )
    elif keyword == "help":
        _send_help_reply(provider, user_id, lead_id, contact, account_phone)

    tdb.append_sms_audit(
        user_id,
        "reply_received",
        lead_id=lead_id,
        metadata={"message_id": message_id, "event_id": event_id},
    )

    if keyword is None and app is not None:
        try:
            from sms_ai_agent import schedule_inbound_ai

            schedule_inbound_ai(
                app,
                user_id,
                lead_id,
                message_id,
                text,
                _normalize_phone(account_phone) or (sender.get("sender_number") or None),
            )
        except Exception:
            logger.exception(
                "SMS_PROCESSING_ERROR stage=ai_schedule messageId=%s leadId=%s",
                message_id,
                lead_id,
            )

    return {"ok": True, "lead_id": lead_id, "message_id": message_id}, 200


def _touch_inbound_lead_state(user_id, lead_id, lead, keyword):
    """Last-activity + pipeline status parity with the Twilio inbound path."""
    from datetime import datetime, timezone

    try:
        db.update_lead_from_analysis(
            lead_id, user_id, last_inbound_at=datetime.now(timezone.utc).isoformat()
        )
        if keyword != "opt_out" and (lead or {}).get("opt_out_status") != "opted_out":
            crm_db.set_lead_status(user_id, lead_id, "contacted", from_automation=True)
    except Exception:
        logger.exception(
            "Failed to update lead activity state lead_id=%s tenant=%s", lead_id, user_id
        )


def _handle_delivery(payload):
    provider = TelnyxSMSProvider()
    event = provider.normalize_delivery_webhook(payload)
    pmid = event.get("provider_message_id")
    if not pmid:
        return {"ok": False, "error": "missing_message_id"}, 400
    row = db.get_sms_message_by_provider_id(str(pmid))
    if not row:
        return {"ok": True, "ignored": True}, 200
    if (row.get("direction") or "outbound") == "inbound":
        # Never let a delivery receipt touch an inbound message row.
        return {"ok": True, "ignored": True}, 200

    status = (event.get("status") or "unknown").lower()
    mapped = {
        "queued": "queued",
        "sending": "submitted",
        "sent": "sent",
        "delivered": "delivered",
        "delivery_failed": "delivery_failed",
        "delivery_unconfirmed": "sent",
        "sending_failed": "failed",
        "expired": "expired",
        "rejected": "rejected",
    }.get(status, status)

    error_message = None
    if mapped in {"failed", "delivery_failed", "rejected", "expired"}:
        code = event.get("error_code")
        error_message = (
            f"Telnyx delivery error {code}" if code else "SMS could not be delivered."
        )
    applied = db.apply_sms_delivery_update(
        row["id"],
        mapped,
        error_message=error_message,
        failure_code=event.get("error_code"),
    )
    logger.info(
        "SMS_DELIVERY_UPDATE messageId=%s telnyxMessageId=%s status=%s applied=%s tenant=%s",
        row["id"],
        pmid,
        mapped,
        applied,
        row.get("user_id"),
    )
    if mapped in {"failed", "delivery_failed", "rejected", "expired"} and row.get("lead_id"):
        crm_db.upsert_needs_attention(
            row["user_id"],
            row["lead_id"],
            "delivery_failed",
            priority="high",
            source_ref_type="sms",
            source_ref_id=row["id"],
        )
        tdb.append_sms_audit(
            row["user_id"],
            "message_failed",
            lead_id=row.get("lead_id"),
            metadata={"provider_message_id": pmid, "status": mapped},
        )
    elif mapped == "delivered":
        tdb.append_sms_audit(
            row["user_id"],
            "message_delivered",
            lead_id=row.get("lead_id"),
            metadata={"provider_message_id": pmid},
        )
    return {"ok": True, "status": applied or mapped}, 200


def _send_help_reply(provider, user_id, lead_id, contact_phone, from_number):
    """Compliance HELP auto-reply for the Telnyx messaging program."""
    import sms_consent

    body = sms_consent.SMS_HELP_RESPONSE
    try:
        result = provider.send_message(
            to_number=contact_phone,
            body=body,
            from_number=from_number or None,
        )
        out_id = db.create_sms_message(
            user_id=user_id,
            persona_id=None,
            provider="telnyx",
            data={
                "lead_name": "SMS Contact",
                "phone_number": contact_phone,
                "message_body": body,
            },
            status="sent",
            lead_id=lead_id,
            direction="outbound",
            consent_status="unknown",
            opt_out_status="active",
        )
        pmid = (result or {}).get("provider_message_id")
        if pmid:
            db.update_sms_message_send_result(out_id, provider_message_id=str(pmid), status="sent")
        tdb.append_sms_audit(
            user_id,
            "HELP_reply_sent",
            lead_id=lead_id,
            metadata={"message_id": out_id},
        )
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "sms_help",
            "HELP keyword auto-reply sent",
            {"message_id": out_id},
        )
    except Exception:
        logger.exception("Failed to send Telnyx HELP reply")


def _apply_opt_out(user_id, lead_id, phone, *, source):
    xdb.apply_opt_out_consent(lead_id, user_id, source=source)
    tdb.add_suppression(user_id, phone, reason="opted_out", source=source, lead_id=lead_id)
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE sms_campaign_jobs
            SET status = 'opted_out', updated_at = ?
            WHERE user_id = ? AND phone_number = ? AND status IN ('pending', 'claimed')
            """,
            (
                __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                user_id,
                phone,
            ),
        )
    tdb.append_sms_audit(user_id, "STOP_received", lead_id=lead_id, metadata={"source": source})
