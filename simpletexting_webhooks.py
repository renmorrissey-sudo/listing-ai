"""SimpleTexting webhook handlers — destination number resolves tenant."""

from __future__ import annotations

import logging

import crm_db
import db
import external_leads_db as xdb
import tenant_sms_db as tdb
from sms_providers.simpletexting import SimpleTextingSMSProvider
from sms_validation import validate_e164_phone

logger = logging.getLogger(__name__)


def _normalize_phone(raw):
    cleaned, err = validate_e164_phone(raw if str(raw or "").startswith("+") else f"+{raw}")
    if cleaned:
        return cleaned
    # Try US 10-digit
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) == 10:
        cleaned, _ = validate_e164_phone(f"+1{digits}")
        return cleaned
    if len(digits) == 11 and digits.startswith("1"):
        cleaned, _ = validate_e164_phone(f"+{digits}")
        return cleaned
    return None


def resolve_tenant_from_account_phone(account_phone):
    if not account_phone:
        return None
    sender = tdb.get_sender_by_number(account_phone)
    return sender


def handle_inbound(payload: dict, *, app=None):
    provider = SimpleTextingSMSProvider()
    event = provider.normalize_inbound_webhook(payload)
    event_id = event.get("event_id") or event.get("provider_message_id")
    if event_id and db.get_sms_message_by_provider_id(str(event_id)):
        return {"ok": True, "duplicate": True}, 200

    sender = resolve_tenant_from_account_phone(event.get("account_phone"))
    if not sender:
        logger.info("ST inbound ignored — unknown accountPhone")
        return {"ok": False, "error": "unknown_destination"}, 404

    user_id = sender["user_id"]
    contact = _normalize_phone(event.get("contact_phone"))
    if not contact:
        return {"ok": False, "error": "invalid_contact"}, 400

    lead = db.get_lead_by_phone(user_id, contact)
    if not lead:
        # Create minimal lead for inbound conversation
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
    keyword = None
    upper = text.upper()
    if upper in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}:
        keyword = "opt_out"
    elif upper in {"START", "UNSTOP", "YES"}:
        keyword = "opt_in"
    elif upper == "HELP":
        keyword = "help"

    message_id = db.create_sms_message(
        user_id=user_id,
        persona_id=None,
        provider="simpletexting",
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
    if event.get("provider_message_id"):
        db.update_sms_message_send_result(
            message_id,
            provider_message_id=str(event["provider_message_id"]),
            status="received",
        )

    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "sms_inbound",
        "Inbound SMS received",
        {"message_id": message_id, "provider": "simpletexting"},
    )
    if keyword in {"opt_in", "help"}:
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            "unreviewed_inbound",
            priority="high",
            source_ref_type="sms",
            source_ref_id=message_id,
        )

    if keyword == "opt_out":
        _apply_opt_out(user_id, lead_id, contact, source="simpletexting_inbound")
    elif keyword == "opt_in":
        db.clear_lead_sms_opt_out(lead_id, user_id)

    tdb.append_sms_audit(
        user_id,
        "reply_received",
        lead_id=lead_id,
        metadata={"message_id": message_id, "event_id": event_id},
    )

    # Defer coach like Twilio path
    if keyword is None and app is not None:
        try:
            from sms_ai_agent import schedule_inbound_ai

            schedule_inbound_ai(
                app,
                user_id,
                lead_id,
                message_id,
                text,
                _normalize_phone(sender.get("sender_number")) or event.get("account_phone"),
            )
        except Exception:
            logger.exception("Failed to schedule coach for ST inbound")

    return {"ok": True, "lead_id": lead_id, "message_id": message_id}, 200


def handle_delivery(payload: dict):
    provider = SimpleTextingSMSProvider()
    event = provider.normalize_delivery_webhook(payload)
    pmid = event.get("provider_message_id")
    if not pmid:
        return {"ok": False, "error": "missing_message_id"}, 400
    row = db.get_sms_message_by_provider_id(str(pmid))
    if not row:
        return {"ok": True, "ignored": True}, 200
    status = event.get("status") or "unknown"
    mapped = {
        "delivered": "delivered",
        "undelivered": "failed",
        "failed": "failed",
        "rejected": "failed",
        "sent": "sent",
        "queued": "queued",
        "submitted": "sent",
    }.get(status, status)
    db.update_sms_message_send_result(row["id"], status=mapped)
    if mapped == "failed" and row.get("lead_id"):
        crm_db.upsert_needs_attention(
            row["user_id"],
            row["lead_id"],
            "delivery_failed",
            priority="high",
            source_ref_type="sms",
            source_ref_id=row["id"],
        )
    return {"ok": True, "status": mapped}, 200


def handle_unsubscribe(payload: dict):
    provider = SimpleTextingSMSProvider()
    event = provider.normalize_unsubscribe_webhook(payload)
    contact = _normalize_phone(event.get("contact_phone"))
    if not contact:
        return {"ok": False, "error": "invalid_phone"}, 400

    sender = resolve_tenant_from_account_phone(event.get("account_phone"))
    targets = []
    if sender:
        targets = [sender["user_id"]]
    else:
        # Fallback: last outbound from_number mapping via sms_messages, else all tenants with this lead
        with db.get_db() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT user_id FROM sms_messages
                WHERE phone_number = ? AND direction = 'outbound'
                ORDER BY id DESC LIMIT 20
                """,
                (contact,),
            ).fetchall()
            targets = [r["user_id"] for r in rows]
        if not targets:
            with db.get_db() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT user_id FROM leads WHERE phone_number = ?",
                    (contact,),
                ).fetchall()
                targets = [r["user_id"] for r in rows]

    for user_id in targets:
        lead = db.get_lead_by_phone(user_id, contact)
        lead_id = lead["id"] if lead else None
        if lead_id:
            _apply_opt_out(user_id, lead_id, contact, source="simpletexting_unsubscribe")
        else:
            tdb.add_suppression(user_id, contact, reason="opted_out", source="simpletexting_unsubscribe")
        tdb.append_sms_audit(
            user_id,
            "unsubscribe_received",
            lead_id=lead_id,
            metadata={"phone": contact},
        )
    return {"ok": True, "tenants": len(targets)}, 200


def _apply_opt_out(user_id, lead_id, phone, *, source):
    xdb.apply_opt_out_consent(lead_id, user_id, source=source)
    tdb.add_suppression(user_id, phone, reason="opted_out", source=source, lead_id=lead_id)
    # Cancel pending campaign jobs for this phone
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE sms_campaign_jobs
            SET status = 'opted_out', updated_at = ?
            WHERE user_id = ? AND phone_number = ? AND status IN ('pending', 'claimed')
            """,
            (__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), user_id, phone),
        )
