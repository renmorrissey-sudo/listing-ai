"""Shared outbound SMS send helper (attestation + authorization + provider)."""

from __future__ import annotations

import logging
import uuid

import config
import db
import tenant_sms_db as tdb
from lead_service import normalize_phone_e164
from sms_authorization import (
    can_send_sms,
    check_telnyx_toll_free_send_allowed,
    record_one_to_one_attestation,
    require_tenant_sender,
)
from sms_provider import sms_status_callback_url
from sms_providers import SmsProviderError, get_sms_provider

logger = logging.getLogger(__name__)

STAGE_VALIDATION = "validation"
STAGE_DATABASE = "database"
STAGE_PROVIDER_REQUEST = "provider_request"
STAGE_PROVIDER_DELIVERY = "provider_delivery"


def _correlation_id():
    return uuid.uuid4().hex[:12]


def _safe_audit(user_id, action, *, actor_user_id=None, lead_id=None, metadata=None):
    try:
        tdb.append_sms_audit(
            user_id,
            action,
            actor_user_id=actor_user_id or user_id,
            lead_id=lead_id,
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("sms audit write failed action=%s user_id=%s", action, user_id)


def _error_payload(
    *,
    error,
    stage,
    correlation_id,
    category="application_error",
    http_status=500,
    message_id=None,
    lead_id=None,
    from_number=None,
    to_number=None,
    provider=None,
    provider_http_status=None,
    provider_code=None,
    provider_message_id=None,
    send_status=None,
    extra=None,
):
    payload = {
        "error": error,
        "stage": stage,
        "error_category": category,
        "correlation_id": correlation_id,
        "provider": provider or (config.SMS_PROVIDER or "unknown"),
        "from_number": from_number,
        "to_number": to_number,
        "send_status": send_status or "failed",
    }
    if message_id is not None:
        payload["id"] = message_id
    if lead_id is not None:
        payload["lead_id"] = lead_id
    if provider_http_status is not None:
        payload["provider_http_status"] = provider_http_status
    if provider_code is not None:
        payload["provider_code"] = provider_code
    if provider_message_id is not None:
        payload["provider_message_id"] = provider_message_id
    if stage == STAGE_PROVIDER_REQUEST and not provider_message_id:
        payload["error"] = (
            f"TopAI could not submit the message to Telnyx. Reference: {correlation_id}."
            if (provider or config.SMS_PROVIDER or "").lower() == "telnyx"
            else f"TopAI could not submit the message to the SMS provider. Reference: {correlation_id}."
        )
    if extra:
        payload.update(extra)
    return payload, http_status


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
    correlation_id = _correlation_id()
    provider_name = (config.SMS_PROVIDER or "unknown").lower()
    to_number = None
    from_number = None

    if not compliance_confirmed:
        payload, status = _error_payload(
            error="Confirm contact SMS consent certification before sending.",
            stage=STAGE_VALIDATION,
            correlation_id=correlation_id,
            category="consent_required",
            http_status=400,
            lead_id=lead_id,
            provider=provider_name,
        )
        return payload, payload["error"], status

    toll_ok, toll_err = check_telnyx_toll_free_send_allowed()
    if not toll_ok:
        payload, status = _error_payload(
            error=toll_err,
            stage=STAGE_VALIDATION,
            correlation_id=correlation_id,
            category="toll_free_verification",
            http_status=403,
            lead_id=lead_id,
            provider=provider_name,
        )
        return payload, payload["error"], status

    try:
        if not tdb.has_accepted_sms_terms(user_id):
            payload, status = _error_payload(
                error=(
                    "Accept TopAI SMS terms before sending. "
                    "Open SMS Diagnostics or Campaigns to accept."
                ),
                stage=STAGE_VALIDATION,
                correlation_id=correlation_id,
                category="terms_required",
                http_status=403,
                lead_id=lead_id,
                provider=provider_name,
            )
            return payload, payload["error"], status

        lead = db.get_lead(lead_id, user_id)
        if not lead:
            payload, status = _error_payload(
                error="Lead not found.",
                stage=STAGE_VALIDATION,
                correlation_id=correlation_id,
                category="not_found",
                http_status=404,
                lead_id=lead_id,
                provider=provider_name,
            )
            return payload, payload["error"], status

        to_number = normalize_phone_e164(lead.get("phone_number") or "")
        if not to_number:
            payload, status = _error_payload(
                error="Lead does not have a valid mobile phone number.",
                stage=STAGE_VALIDATION,
                correlation_id=correlation_id,
                category="invalid_destination",
                http_status=400,
                lead_id=lead_id,
                provider=provider_name,
            )
            return payload, payload["error"], status

        att_id, att_err = record_one_to_one_attestation(
            user_id,
            lead_id,
            message_body=message_body,
            source_page=source_page,
            message_purpose=message_purpose,
        )
        if att_err:
            payload, status = _error_payload(
                error=att_err,
                stage=STAGE_VALIDATION,
                correlation_id=correlation_id,
                category="consent_required",
                http_status=403,
                lead_id=lead_id,
                to_number=to_number,
                provider=provider_name,
            )
            return payload, payload["error"], status

        allowed, block_msg = can_send_sms(
            user_id,
            lead_id,
            message_purpose=message_purpose,
            message_body=message_body,
            skip_quiet_hours=skip_quiet_hours,
        )
        if not allowed:
            payload, status = _error_payload(
                error=block_msg,
                stage=STAGE_VALIDATION,
                correlation_id=correlation_id,
                category="send_blocked",
                http_status=403,
                lead_id=lead_id,
                to_number=to_number,
                provider=provider_name,
            )
            _safe_audit(
                user_id,
                "message_send_blocked",
                lead_id=lead_id,
                metadata={
                    "correlation_id": correlation_id,
                    "stage": STAGE_VALIDATION,
                    "to_number": to_number,
                    "error": block_msg,
                    "compliance_confirmed": True,
                },
            )
            return payload, payload["error"], status

        sender, sender_err = require_tenant_sender(user_id)
        if sender_err:
            payload, status = _error_payload(
                error=sender_err,
                stage=STAGE_VALIDATION,
                correlation_id=correlation_id,
                category="sender_missing",
                http_status=403,
                lead_id=lead_id,
                to_number=to_number,
                provider=provider_name,
            )
            return payload, payload["error"], status

        from_number = normalize_phone_e164(
            sender.get("sender_number") or getattr(config, "TELNYX_PHONE_NUMBER", "") or ""
        )

        if message_id is None:
            message_id = db.create_sms_message(
                user_id=user_id,
                persona_id=persona_id,
                provider=config.SMS_PROVIDER,
                data={
                    "lead_name": lead.get("name"),
                    "phone_number": to_number,
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
            "SMS pre-provider failure correlation_id=%s user_id=%s lead_id=%s",
            correlation_id,
            user_id,
            lead_id,
        )
        payload, status = _error_payload(
            error=(
                f"TopAI could not prepare the SMS send. Reference: {correlation_id}."
            ),
            stage=STAGE_DATABASE,
            correlation_id=correlation_id,
            category="database_error",
            http_status=500,
            message_id=message_id,
            lead_id=lead_id,
            from_number=from_number,
            to_number=to_number,
            provider=provider_name,
        )
        _safe_audit(
            user_id,
            "message_send_failed",
            lead_id=lead_id,
            metadata={
                "correlation_id": correlation_id,
                "stage": STAGE_DATABASE,
                "from_number": from_number,
                "to_number": to_number,
                "compliance_confirmed": bool(compliance_confirmed),
                "provider": provider_name,
            },
        )
        return payload, payload["error"], status

    provider = get_sms_provider()
    if not provider.is_configured():
        payload, status = _error_payload(
            error="SMS provider is not configured.",
            stage=STAGE_VALIDATION,
            correlation_id=correlation_id,
            category="provider_not_configured",
            http_status=503,
            message_id=message_id,
            lead_id=lead_id,
            from_number=from_number,
            to_number=to_number,
            provider=provider_name,
        )
        return payload, payload["error"], status

    _safe_audit(
        user_id,
        "message_send_attempt",
        lead_id=lead_id,
        metadata={
            "correlation_id": correlation_id,
            "stage": STAGE_PROVIDER_REQUEST,
            "message_id": message_id,
            "from_number": from_number,
            "to_number": to_number,
            "provider": provider_name,
            "compliance_confirmed": True,
            "source_page": source_page,
        },
    )

    try:
        result = provider.send_sms(
            to_number,
            message_body,
            status_callback=sms_status_callback_url(),
            from_number=from_number or None,
        )
    except TypeError:
        # Legacy Twilio signature without from_number
        try:
            result = provider.send_sms(
                to_number,
                message_body,
                status_callback=sms_status_callback_url(),
            )
        except SmsProviderError as exc:
            return _provider_error_result(
                exc,
                user_id=user_id,
                message_id=message_id,
                lead_id=lead_id,
                from_number=from_number,
                to_number=to_number,
                correlation_id=correlation_id,
                provider_name=provider_name,
            )
        except Exception:
            logger.exception(
                "Unexpected SMS send failure correlation_id=%s", correlation_id
            )
            return _network_error_result(
                user_id=user_id,
                message_id=message_id,
                lead_id=lead_id,
                from_number=from_number,
                to_number=to_number,
                correlation_id=correlation_id,
                provider_name=provider_name,
            )
    except SmsProviderError as exc:
        return _provider_error_result(
            exc,
            user_id=user_id,
            message_id=message_id,
            lead_id=lead_id,
            from_number=from_number,
            to_number=to_number,
            correlation_id=correlation_id,
            provider_name=provider_name,
        )
    except Exception:
        logger.exception("Unexpected SMS send failure correlation_id=%s", correlation_id)
        return _network_error_result(
            user_id=user_id,
            message_id=message_id,
            lead_id=lead_id,
            from_number=from_number,
            to_number=to_number,
            correlation_id=correlation_id,
            provider_name=provider_name,
        )

    send_status = result.get("status") or "queued"
    provider_message_id = result.get("provider_message_id")
    try:
        db.update_sms_message_send_result(
            message_id,
            provider_message_id=provider_message_id,
            status=send_status,
        )
        db.set_lead_consent(lead_id, user_id, "confirmed")
        db.touch_lead_outbound(lead_id, user_id)
    except Exception:
        logger.exception(
            "SMS post-send persistence failed correlation_id=%s message_id=%s",
            correlation_id,
            message_id,
        )
        # Provider accepted the message — do not claim total failure.
        _safe_audit(
            user_id,
            "message_send_persist_failed",
            lead_id=lead_id,
            metadata={
                "correlation_id": correlation_id,
                "stage": STAGE_DATABASE,
                "message_id": message_id,
                "provider_message_id": provider_message_id,
                "from_number": from_number,
                "to_number": to_number,
                "send_status": send_status,
                "provider": provider_name,
            },
        )
        return {
            "id": message_id,
            "lead_id": lead_id,
            "status": send_status,
            "provider_message_id": provider_message_id,
            "message_body": message_body,
            "from_number": from_number,
            "to_number": to_number,
            "correlation_id": correlation_id,
            "stage": STAGE_PROVIDER_DELIVERY,
            "warning": (
                "Message was accepted by the provider but TopAI could not fully "
                f"save the send record. Reference: {correlation_id}."
            ),
            "attestation_id": att_id,
        }, None, 201

    _safe_audit(
        user_id,
        "message_sent",
        lead_id=lead_id,
        metadata={
            "correlation_id": correlation_id,
            "stage": STAGE_PROVIDER_DELIVERY,
            "message_id": message_id,
            "provider_message_id": provider_message_id,
            "attestation_id": att_id,
            "from_number": from_number,
            "to_number": to_number,
            "send_status": send_status,
            "provider": provider_name,
            "compliance_confirmed": True,
        },
    )
    return {
        "id": message_id,
        "lead_id": lead_id,
        "status": send_status,
        "provider_message_id": provider_message_id,
        "message_body": message_body,
        "from_number": from_number,
        "to_number": to_number,
        "attestation_id": att_id,
        "correlation_id": correlation_id,
        "stage": STAGE_PROVIDER_DELIVERY,
        "send_status": send_status,
    }, None, 201


def _provider_error_result(
    exc,
    *,
    user_id,
    message_id,
    lead_id,
    from_number,
    to_number,
    correlation_id,
    provider_name,
):
    safe = str(exc)
    try:
        db.update_sms_message_send_result(
            message_id, status="failed", error_message=safe[:500]
        )
    except Exception:
        logger.exception(
            "failed to persist provider error correlation_id=%s", correlation_id
        )
    stage = (
        STAGE_PROVIDER_REQUEST
        if getattr(exc, "status_code", None) in {None, 0}
        or (getattr(exc, "status_code", None) or 0) >= 500
        else STAGE_PROVIDER_REQUEST
    )
    # 4xx = Telnyx rejected; connection/5xx = could not submit cleanly
    if getattr(exc, "status_code", None) and 400 <= int(exc.status_code) < 500:
        error = safe
        category = "provider_rejected"
    else:
        error = (
            f"TopAI could not submit the message to Telnyx. Reference: {correlation_id}."
            if provider_name == "telnyx"
            else f"TopAI could not submit the message to the SMS provider. Reference: {correlation_id}."
        )
        category = "provider_request_failed"
        stage = STAGE_PROVIDER_REQUEST
    payload, status = _error_payload(
        error=error,
        stage=stage,
        correlation_id=correlation_id,
        category=category,
        http_status=503,
        message_id=message_id,
        lead_id=lead_id,
        from_number=from_number,
        to_number=to_number,
        provider=provider_name,
        provider_http_status=getattr(exc, "status_code", None),
        provider_code=getattr(exc, "provider_code", None),
        send_status="failed",
        extra=exc.to_public_dict() if hasattr(exc, "to_public_dict") else None,
    )
    # Prefer the safer stage-specific message over raw provider dict error for non-4xx.
    if category != "provider_rejected":
        payload["error"] = error
    _safe_audit(
        user_id,
        "message_send_failed",
        lead_id=lead_id,
        metadata={
            "correlation_id": correlation_id,
            "stage": stage,
            "message_id": message_id,
            "from_number": from_number,
            "to_number": to_number,
            "provider": provider_name,
            "provider_http_status": getattr(exc, "status_code", None),
            "provider_code": getattr(exc, "provider_code", None),
            "error_category": category,
        },
    )
    return payload, payload["error"], status


def _network_error_result(
    *,
    user_id,
    message_id,
    lead_id,
    from_number,
    to_number,
    correlation_id,
    provider_name,
):
    error = (
        f"TopAI could not submit the message to Telnyx. Reference: {correlation_id}."
        if provider_name == "telnyx"
        else f"TopAI could not submit the message to the SMS provider. Reference: {correlation_id}."
    )
    try:
        db.update_sms_message_send_result(
            message_id, status="failed", error_message=error[:500]
        )
    except Exception:
        logger.exception(
            "failed to persist network error correlation_id=%s", correlation_id
        )
    payload, status = _error_payload(
        error=error,
        stage=STAGE_PROVIDER_REQUEST,
        correlation_id=correlation_id,
        category="provider_request_failed",
        http_status=500,
        message_id=message_id,
        lead_id=lead_id,
        from_number=from_number,
        to_number=to_number,
        provider=provider_name,
        send_status="failed",
    )
    _safe_audit(
        user_id,
        "message_send_failed",
        lead_id=lead_id,
        metadata={
            "correlation_id": correlation_id,
            "stage": STAGE_PROVIDER_REQUEST,
            "message_id": message_id,
            "from_number": from_number,
            "to_number": to_number,
            "provider": provider_name,
            "error_category": "provider_request_failed",
        },
    )
    return payload, payload["error"], status
