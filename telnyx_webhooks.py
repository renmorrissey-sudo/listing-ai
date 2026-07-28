"""Telnyx Messaging Profile webhook — Ed25519 verified, fast ack, async coach."""

from __future__ import annotations

import logging
import threading

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
    Returns (result_dict, http_status).
    """
    event_type = _event_type(payload)
    data = (payload or {}).get("data") or {}
    event_id = data.get("id")

    if event_id and tdb.get_webhook_event_by_provider_id("telnyx", str(event_id)):
        return {"ok": True, "duplicate": True}, 200

    tdb.record_webhook_event(
        provider="telnyx",
        provider_event_id=str(event_id) if event_id else None,
        event_type=event_type or "unknown",
        provider_message_id=((data.get("payload") or {}).get("id")),
        safe_metadata={"event_type": event_type},
    )

    if event_type == "message.received":
        return _handle_received(payload, app=app)
    if event_type in {"message.sent", "message.finalized"}:
        return _handle_delivery(payload)
    # Unknown — ack so Telnyx does not retry forever
    logger.info("Telnyx webhook ignored event_type=%s", event_type or "none")
    return {"ok": True, "ignored": True}, 200


def _handle_received(payload, *, app=None):
    provider = TelnyxSMSProvider()
    event = provider.normalize_inbound_webhook(payload)
    pmid = event.get("provider_message_id")
    if pmid and db.get_sms_message_by_provider_id(str(pmid)):
        return {"ok": True, "duplicate": True}, 200

    account_phone = event.get("account_phone")
    sender = tdb.get_sender_by_number(account_phone)
    if not sender:
        # Trial: match global TELNYX_PHONE_NUMBER
        import config

        if config.TELNYX_PHONE_NUMBER and _normalize_phone(account_phone) == _normalize_phone(
            config.TELNYX_PHONE_NUMBER
        ):
            # Route to first active telnyx sender or leave unmatched for review
            sender = tdb.get_any_telnyx_sender()
        if not sender:
            logger.info("Telnyx inbound ignored — unknown destination")
            return {"ok": False, "error": "unknown_destination"}, 404

    user_id = sender["user_id"]
    contact = _normalize_phone(event.get("contact_phone"))
    if not contact:
        return {"ok": False, "error": "invalid_contact"}, 400

    lead = db.get_lead_by_phone(user_id, contact)
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
    else:
        lead_id = lead["id"]

    text = (event.get("text") or "").strip()
    upper = text.upper().strip()
    keyword = None
    if upper in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}:
        keyword = "opt_out"
    elif upper in {"START", "UNSTOP", "YES"}:
        keyword = "opt_in"
    elif upper == "HELP":
        keyword = "help"

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
        metadata={"message_id": message_id, "event_id": event.get("event_id")},
    )
    tdb.mark_webhook_processed("telnyx", str(event.get("event_id") or ""), "processed")

    if keyword is None and app is not None:
        try:
            from sms_inbound import _schedule_coach

            _schedule_coach(app, user_id, lead_id, message_id, text)
        except Exception:
            logger.exception("Failed to schedule coach for Telnyx inbound")

    return {"ok": True, "lead_id": lead_id, "message_id": message_id}, 200


def _handle_delivery(payload):
    provider = TelnyxSMSProvider()
    event = provider.normalize_delivery_webhook(payload)
    pmid = event.get("provider_message_id")
    if not pmid:
        return {"ok": False, "error": "missing_message_id"}, 400
    row = db.get_sms_message_by_provider_id(str(pmid))
    if not row:
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
    db.update_sms_message_send_result(row["id"], status=mapped)
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
    if event.get("event_id"):
        tdb.mark_webhook_processed("telnyx", str(event["event_id"]), "processed")
    return {"ok": True, "status": mapped}, 200


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
