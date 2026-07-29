"""Shared outbound SMS send helper (attestation + authorization + provider)."""

from __future__ import annotations

import logging

import config
import db
import tenant_sms_db as tdb
from sms_authorization import (
    can_send_sms,
    check_telnyx_toll_free_send_allowed,
    record_one_to_one_attestation,
    require_tenant_sender,
)
from sms_provider import sms_status_callback_url
from sms_providers import SmsProviderError, get_sms_provider

logger = logging.getLogger(__name__)


def send_authorized_sms(
    user_id,
    lead_id,
    message_body,
    *,
    source_page,
    compliance_confirmed=False,
    message_purpose="real_estate_follow_up",
    persona_id=None,
    message_id=None,
    skip_quiet_hours=False,
):
    """
    Record attestation (if confirmed), authorize, send via active provider.
    Returns (result_dict, error_str, http_status).
    """
    if not compliance_confirmed:
        return None, "Confirm contact SMS consent certification before sending.", 400

    toll_ok, toll_err = check_telnyx_toll_free_send_allowed()
    if not toll_ok:
        return None, toll_err, 403

    if not tdb.has_accepted_sms_terms(user_id):
        return (
            None,
            "Accept TopAI SMS terms before sending. Open SMS Diagnostics or Campaigns to accept.",
            403,
        )

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found.", 404

    att_id, att_err = record_one_to_one_attestation(
        user_id,
        lead_id,
        message_body=message_body,
        source_page=source_page,
        message_purpose=message_purpose,
    )
    if att_err:
        return None, att_err, 403

    allowed, block_msg = can_send_sms(
        user_id,
        lead_id,
        message_purpose=message_purpose,
        message_body=message_body,
        skip_quiet_hours=skip_quiet_hours,
    )
    if not allowed:
        return None, block_msg, 403

    sender, sender_err = require_tenant_sender(user_id)
    if sender_err:
        return None, sender_err, 403

    if message_id is None:
        message_id = db.create_sms_message(
            user_id=user_id,
            persona_id=persona_id,
            provider=config.SMS_PROVIDER,
            data={
                "lead_name": lead.get("name"),
                "phone_number": lead.get("phone_number"),
                "lead_type": lead.get("lead_type"),
                "property_interest": lead.get("property_interest"),
                "message_body": message_body,
            },
            status="draft",
            lead_id=lead_id,
            direction="outbound",
            consent_status="confirmed",
            opt_out_status=lead.get("opt_out_status") or "active",
        )

    provider = get_sms_provider()
    if not provider.is_configured():
        return None, "SMS provider is not configured.", 503

    from_number = sender.get("sender_number")
    try:
        result = provider.send_sms(
            lead["phone_number"],
            message_body,
            status_callback=sms_status_callback_url(),
            from_number=from_number,
        )
    except TypeError:
        # Legacy Twilio signature without from_number
        result = provider.send_sms(
            lead["phone_number"],
            message_body,
            status_callback=sms_status_callback_url(),
        )
    except SmsProviderError as exc:
        db.update_sms_message_send_result(message_id, status="failed", error_message=str(exc))
        return {"id": message_id, "error": str(exc), **exc.to_public_dict()}, str(exc), 503
    except Exception:
        logger.exception("Unexpected SMS send failure")
        safe = "SMS could not be sent due to an internal application error."
        db.update_sms_message_send_result(message_id, status="failed", error_message=safe)
        return {"id": message_id, "error": safe}, safe, 500

    db.update_sms_message_send_result(
        message_id,
        provider_message_id=result["provider_message_id"],
        status=result.get("status") or "queued",
    )
    db.set_lead_consent(lead_id, user_id, "confirmed")
    db.touch_lead_outbound(lead_id, user_id)
    tdb.append_sms_audit(
        user_id,
        "message_sent",
        actor_user_id=user_id,
        lead_id=lead_id,
        metadata={
            "message_id": message_id,
            "provider_message_id": result.get("provider_message_id"),
            "attestation_id": att_id,
            "from_number": from_number,
        },
    )
    return {
        "id": message_id,
        "lead_id": lead_id,
        "status": result.get("status") or "queued",
        "provider_message_id": result["provider_message_id"],
        "message_body": message_body,
        "from_number": from_number,
        "attestation_id": att_id,
    }, None, 201
