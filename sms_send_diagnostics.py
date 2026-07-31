"""Safe, non-secret helpers for SMS send diagnostics / logging."""

from __future__ import annotations


def safe_telnyx_payload(*, from_number, to_number, text, messaging_profile_id=None, webhook_url=None):
    """Non-secret view of the outbound Telnyx Messages API body (never includes credentials)."""
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
