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
from sms_send_diagnostics import log_attempt, new_attempt, public_fields, set_stage

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
    correlation_id=None,
    diagnostics=None,
):
    """
    Record attestation (if confirmed), authorize, send via active provider.
    Returns (result_dict, error_str, http_status).
    """
    diag = diagnostics or new_attempt(
        correlation_id=correlation_id, source_page=source_page
    )
    set_stage(diag, "validation", provider=(config.SMS_PROVIDER or "").lower())

    if not compliance_confirmed:
        set_stage(diag, "consent", error="compliance_not_confirmed")
        log_attempt(diag, level=logging.WARNING)
        return None, "Confirm contact SMS consent certification before sending.", 400

    set_stage(diag, "toll_free")
    toll_ok, toll_err = check_telnyx_toll_free_send_allowed()
    if not toll_ok:
        set_stage(diag, "toll_free", error="toll_free_not_verified")
        log_attempt(diag, level=logging.WARNING)
        return None, toll_err, 403

    set_stage(diag, "terms")
    if not tdb.has_accepted_sms_terms(user_id):
        set_stage(diag, "terms", error="sms_terms_not_accepted")
        log_attempt(diag, level=logging.WARNING)
        return (
            None,
            "Accept TopAI SMS terms before sending. Open SMS Diagnostics or Campaigns to accept.",
            403,
        )

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        set_stage(diag, "authorization", lead_id=lead_id, error="lead_not_found")
        log_attempt(diag, level=logging.WARNING)
        return None, "Lead not found.", 404

    set_stage(
        diag,
        "authorization",
        lead_id=lead_id,
        normalized_destination=lead.get("phone_number"),
    )

    att_id, att_err = record_one_to_one_attestation(
        user_id,
        lead_id,
        message_body=message_body,
        source_page=source_page,
        message_purpose=message_purpose,
    )
    if att_err:
        set_stage(diag, "attestation", error="attestation_failed")
        log_attempt(diag, level=logging.WARNING, extra=att_err)
        return {**public_fields(diag)}, att_err, 403

    allowed, block_msg = can_send_sms(
        user_id,
        lead_id,
        message_purpose=message_purpose,
        message_body=message_body,
        skip_quiet_hours=skip_quiet_hours,
    )
    if not allowed:
        set_stage(diag, "authorization", error="send_blocked")
        log_attempt(diag, level=logging.WARNING, extra=block_msg)
        return {**public_fields(diag)}, block_msg, 403

    sender, sender_err = require_tenant_sender(user_id)
    if sender_err:
        set_stage(diag, "authorization", error="no_sender")
        log_attempt(diag, level=logging.WARNING)
        return {**public_fields(diag)}, sender_err, 403

    from_number = sender.get("sender_number")
    set_stage(diag, "db_record", from_number=from_number)

    if message_id is None:
        try:
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
        except Exception:
            logger.exception(
                "SMS audit/db_record failed before provider call correlation_id=%s",
                diag.get("correlation_id"),
            )
            set_stage(diag, "db_record", error="create_message_failed", reached_provider=False)
            log_attempt(diag, level=logging.ERROR)
            try:
                tdb.append_sms_audit(
                    user_id,
                    "message_send_failed",
                    actor_user_id=user_id,
                    lead_id=lead_id,
                    metadata={**public_fields(diag), "reason": "db_record_before_provider"},
                )
            except Exception:
                logger.exception("Failed to append SMS audit after db_record failure")
            return (
                {**public_fields(diag)},
                "SMS could not be sent due to an internal application error.",
                500,
            )

    set_stage(diag, "provider_request", message_id=message_id, from_number=from_number)

    provider = get_sms_provider()
    set_stage(diag, "provider_request", provider=getattr(provider, "name", config.SMS_PROVIDER))
    if not provider.is_configured():
        set_stage(diag, "provider_request", error="provider_not_configured")
        log_attempt(diag, level=logging.ERROR)
        return {**public_fields(diag)}, "SMS provider is not configured.", 503

    try:
        set_stage(diag, "provider_request", reached_provider=True)
        log_attempt(diag)
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
        set_stage(
            diag,
            "provider_response",
            reached_provider=True,
            provider_http_status=getattr(exc, "status_code", None),
            provider_error_code=getattr(exc, "provider_code", None),
            error="provider_rejected",
        )
        log_attempt(diag, level=logging.ERROR)
        try:
            db.update_sms_message_send_result(
                message_id, status="failed", error_message=str(exc)
            )
        except Exception:
            logger.exception(
                "Failed to record provider rejection correlation_id=%s",
                diag.get("correlation_id"),
            )
        try:
            tdb.append_sms_audit(
                user_id,
                "message_send_failed",
                actor_user_id=user_id,
                lead_id=lead_id,
                metadata={**public_fields(diag), "reason": "provider_error"},
            )
        except Exception:
            logger.exception("Failed to append SMS audit after provider error")
        return {
            "id": message_id,
            "error": str(exc),
            **exc.to_public_dict(),
            **public_fields(diag),
        }, str(exc), 503
    except Exception:
        logger.exception(
            "Unexpected SMS send failure correlation_id=%s", diag.get("correlation_id")
        )
        safe = "SMS could not be sent due to an internal application error."
        set_stage(
            diag,
            "provider_response",
            reached_provider=True,
            error="provider_exception",
        )
        log_attempt(diag, level=logging.ERROR)
        try:
            db.update_sms_message_send_result(
                message_id, status="failed", error_message=safe
            )
        except Exception:
            logger.exception(
                "Failed to record unexpected send failure correlation_id=%s",
                diag.get("correlation_id"),
            )
        return {"id": message_id, "error": safe, **public_fields(diag)}, safe, 500

    set_stage(
        diag,
        "provider_response",
        reached_provider=True,
        provider_message_id=result.get("provider_message_id"),
        provider_http_status=200,
    )

    try:
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
                **public_fields(diag),
            },
        )
    except Exception:
        logger.exception(
            "Post-send DB update failed after provider accepted message "
            "correlation_id=%s provider_message_id=%s",
            diag.get("correlation_id"),
            result.get("provider_message_id"),
        )
        set_stage(diag, "db_record", error="post_send_db_failed")
        log_attempt(diag, level=logging.ERROR)
        # Provider already accepted — surface success with diagnostic warning.
        return {
            "id": message_id,
            "lead_id": lead_id,
            "status": result.get("status") or "queued",
            "provider_message_id": result["provider_message_id"],
            "message_body": message_body,
            "from_number": from_number,
            "attestation_id": att_id,
            "warning": "Message was accepted by the provider but local recording failed.",
            **public_fields(diag),
        }, None, 201

    set_stage(diag, "complete")
    log_attempt(diag)
    return {
        "id": message_id,
        "lead_id": lead_id,
        "status": result.get("status") or "queued",
        "provider_message_id": result["provider_message_id"],
        "message_body": message_body,
        "from_number": from_number,
        "attestation_id": att_id,
        **public_fields(diag),
    }, None, 201
