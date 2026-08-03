"""Normalized SMS outbound status model and safe UI diagnostics helpers."""

from __future__ import annotations

from datetime import datetime

# Application status model (stored on sms_messages.status for outbound attempts).
STATUS_PREPARING = "preparing"
STATUS_QUEUED = "queued"
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_DELIVERY_FAILED = "delivery_failed"
STATUS_REJECTED = "rejected"
STATUS_PROVIDER_ERROR = "provider_error"
STATUS_DATABASE_ERROR = "database_error"

APP_STATUSES = frozenset(
    {
        STATUS_PREPARING,
        STATUS_QUEUED,
        STATUS_SENT,
        STATUS_DELIVERED,
        STATUS_DELIVERY_FAILED,
        STATUS_REJECTED,
        STATUS_PROVIDER_ERROR,
        STATUS_DATABASE_ERROR,
        # Legacy values retained for existing rows / drafts / inbound.
        "draft",
        "failed",
        "received",
        "suggested",
        "cancelled",
        "expired",
        "submitted",
    }
)

TERMINAL_SUCCESS = frozenset({STATUS_DELIVERED})
TERMINAL_FAILURE = frozenset(
    {
        STATUS_DELIVERY_FAILED,
        STATUS_REJECTED,
        STATUS_PROVIDER_ERROR,
        STATUS_DATABASE_ERROR,
        "failed",
        "expired",
    }
)
IN_FLIGHT = frozenset({STATUS_PREPARING, STATUS_QUEUED, STATUS_SENT, "submitted"})

# Telnyx webhook / API statuses → application status.
TELNYX_STATUS_MAP = {
    "queued": STATUS_QUEUED,
    "sending": STATUS_QUEUED,
    "submitted": STATUS_QUEUED,
    "sent": STATUS_SENT,
    "delivered": STATUS_DELIVERED,
    "delivery_failed": STATUS_DELIVERY_FAILED,
    "delivery_unconfirmed": STATUS_SENT,
    "sending_failed": STATUS_PROVIDER_ERROR,
    "expired": "expired",
    "rejected": STATUS_REJECTED,
    "failed": STATUS_PROVIDER_ERROR,
}


def normalize_provider_status(raw_status, *, event_type=None) -> str:
    """Normalize a Telnyx (or legacy) provider status into the app status model."""
    status = (raw_status or "").strip().lower()
    event = (event_type or "").strip().lower()
    if event == "message.sent" and status in {"", "unknown"}:
        status = "sent"
    if event == "message.delivery_failed":
        status = "delivery_failed"
    if event == "message.finalized" and status in {"", "unknown"}:
        # Finalized without an explicit to[].status — do not invent delivered.
        status = "sent"
    mapped = TELNYX_STATUS_MAP.get(status)
    if mapped:
        return mapped
    if status in APP_STATUSES:
        return status
    return status or STATUS_QUEUED


def status_user_label(status: str | None) -> str:
    """Short human label for status panel / notices."""
    s = (status or "").strip().lower()
    return {
        STATUS_PREPARING: "preparing",
        STATUS_QUEUED: "queued",
        STATUS_SENT: "sent",
        STATUS_DELIVERED: "delivered",
        STATUS_DELIVERY_FAILED: "delivery failed",
        STATUS_REJECTED: "rejected",
        STATUS_PROVIDER_ERROR: "provider error",
        STATUS_DATABASE_ERROR: "database error",
        "failed": "provider error",
        "submitted": "queued",
        "expired": "expired",
        "draft": "draft",
    }.get(s, s or "unknown")


def success_notice_copy(status: str | None, *, to_display: str, message_id: str | None, submitted_at=None, delivered_at=None) -> dict:
    """Build safe success-panel copy for submitted vs delivered."""
    s = normalize_provider_status(status)
    mid = (message_id or "").strip() or None
    if s == STATUS_DELIVERED:
        return {
            "kind": "delivered",
            "title": "SMS delivered successfully.",
            "status": STATUS_DELIVERED,
            "to_display": to_display,
            "message_id": mid,
            "delivered_at": delivered_at,
            "submitted_at": submitted_at,
        }
    if s == STATUS_SENT:
        return {
            "kind": "sent",
            "title": "SMS sent successfully.",
            "status": STATUS_SENT,
            "to_display": to_display,
            "message_id": mid,
            "submitted_at": submitted_at,
            "delivered_at": None,
        }
    # Default: accepted by provider, awaiting delivery confirmation
    return {
        "kind": "submitted",
        "title": "SMS submitted successfully to Telnyx.",
        "status": STATUS_QUEUED if s in {STATUS_QUEUED, STATUS_PREPARING, "submitted"} else s,
        "to_display": to_display,
        "message_id": mid,
        "submitted_at": submitted_at,
        "delivered_at": None,
    }


def format_phone_display(e164: str | None) -> str:
    """Readable US display; keep non-US E.164 as-is."""
    raw = (e164 or "").strip()
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"+1 ({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    return raw if raw.startswith("+") else f"+{digits}" if digits else raw


def _iso_or_none(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def latest_outbound_diagnostics(row: dict | None) -> dict:
    """
    Safe diagnostic fields for the AI SMS Assistant status panel.
    Never includes secrets, raw provider payloads, or headers.
    """
    if not row:
        return {
            "has_outbound": False,
            "latest_send_status": None,
            "latest_sms_status": None,
            "latest_sms_status_label": None,
            "latest_sms_destination": None,
            "latest_sms_destination_display": None,
            "latest_sms_submitted_at": None,
            "latest_sms_delivered_at": None,
            "latest_sms_failed_at": None,
            "latest_sms_message_id": None,
            "latest_telnyx_error_code": None,
            "latest_error_code": None,
            "latest_telnyx_error_message": None,
            "latest_error_message": None,
            "latest_correlation_id": None,
            "empty_state_message": "No SMS has been sent yet.",
        }

    status = normalize_provider_status(row.get("status"))
    # Prefer destination columns; phone_number is the historical outbound destination.
    destination = (
        row.get("to_number")
        or row.get("phone_number")
        or ""
    )
    failure_code = row.get("failure_code")
    err_msg = row.get("error_message")
    # Only surface error fields for failure states.
    is_error = status in TERMINAL_FAILURE or bool(failure_code)
    safe_code = str(failure_code) if failure_code not in (None, "") and is_error else None
    safe_err = (str(err_msg)[:240] if err_msg and is_error else None)
    correlation = row.get("correlation_id") if is_error else None

    submitted = (
        _iso_or_none(row.get("submitted_at"))
        or _iso_or_none(row.get("sent_at"))
        or _iso_or_none(row.get("created_at"))
    )
    delivered = _iso_or_none(row.get("delivered_at")) if status == STATUS_DELIVERED else _iso_or_none(row.get("delivered_at"))
    failed = _iso_or_none(row.get("failed_at")) if is_error else None

    return {
        "has_outbound": True,
        "latest_send_status": status,
        "latest_sms_status": status,
        "latest_sms_status_label": status_user_label(status),
        "latest_sms_destination": destination or None,
        "latest_sms_destination_display": format_phone_display(destination) or None,
        "latest_sms_submitted_at": submitted,
        "latest_sms_delivered_at": delivered,
        "latest_sms_failed_at": failed,
        "latest_sms_message_id": row.get("provider_message_id"),
        "latest_sms_from": row.get("from_number"),
        "latest_sms_from_display": format_phone_display(row.get("from_number")) or None,
        "latest_sms_id": row.get("id"),
        "latest_sms_lead_id": row.get("lead_id"),
        "latest_telnyx_error_code": safe_code,
        "latest_error_code": safe_code,
        "latest_telnyx_error_message": safe_err,
        "latest_error_message": safe_err,
        "latest_correlation_id": correlation,
        "empty_state_message": None,
    }
