"""Safe, non-secret diagnostics for outbound SMS send attempts."""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

STAGES = (
    "validation",
    "lead_upsert",
    "authorization",
    "db_record",
    "provider_request",
    "provider_response",
    "complete",
)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def new_attempt(*, correlation_id: str | None = None, source_page: str | None = None) -> dict:
    return {
        "correlation_id": correlation_id or new_correlation_id(),
        "source_page": source_page,
        "stage": "validation",
        "normalized_destination": None,
        "from_number": None,
        "provider": None,
        "reached_provider": False,
        "provider_http_status": None,
        "provider_error_code": None,
        "provider_message_id": None,
        "message_id": None,
        "lead_id": None,
    }


def set_stage(diag: dict, stage: str, **fields: Any) -> dict:
    if stage not in STAGES and stage not in {
        "consent",
        "terms",
        "toll_free",
        "attestation",
        "failed",
    }:
        stage = stage or "failed"
    diag["stage"] = stage
    for key, value in fields.items():
        if key in diag or key in {
            "normalized_destination",
            "from_number",
            "provider",
            "reached_provider",
            "provider_http_status",
            "provider_error_code",
            "provider_message_id",
            "message_id",
            "lead_id",
            "error",
        }:
            diag[key] = value
    return diag


def public_fields(diag: dict | None) -> dict:
    """Subset safe to return to the browser / store in audit metadata."""
    if not diag:
        return {}
    return {
        "correlation_id": diag.get("correlation_id"),
        "stage": diag.get("stage"),
        "normalized_destination": diag.get("normalized_destination"),
        "from_number": diag.get("from_number"),
        "provider": diag.get("provider"),
        "reached_provider": bool(diag.get("reached_provider")),
        "provider_http_status": diag.get("provider_http_status"),
        "provider_error_code": diag.get("provider_error_code"),
        "provider_message_id": diag.get("provider_message_id"),
        "message_id": diag.get("message_id"),
        "lead_id": diag.get("lead_id"),
    }


def log_attempt(diag: dict, *, level: int = logging.INFO, extra: str | None = None) -> None:
    payload = public_fields(diag)
    # Never log message body or credentials.
    logger.log(
        level,
        "sms_send_attempt correlation_id=%s stage=%s provider=%s to=%s from=%s "
        "reached_provider=%s http=%s provider_code=%s provider_message_id=%s message_id=%s%s",
        payload.get("correlation_id"),
        payload.get("stage"),
        payload.get("provider"),
        payload.get("normalized_destination"),
        payload.get("from_number"),
        payload.get("reached_provider"),
        payload.get("provider_http_status"),
        payload.get("provider_error_code"),
        payload.get("provider_message_id"),
        payload.get("message_id"),
        f" {extra}" if extra else "",
    )


def safe_telnyx_payload(*, from_number, to_number, text, messaging_profile_id=None, webhook_url=None):
    """Non-secret view of the outbound Telnyx Messages API body."""
    payload = {
        "from": from_number,
        "to": to_number,
        "text_chars": len(text or ""),
        "type": "SMS",
    }
    if messaging_profile_id:
        payload["messaging_profile_id_configured"] = True
    if webhook_url:
        payload["webhook_url"] = webhook_url
    return payload
