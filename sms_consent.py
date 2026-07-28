"""Public SMS consent / real-estate inquiry form helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import db
from sms_validation import validate_e164_phone

# Exact disclosure shown on /sms-consent (versioned for audit).
SMS_CONSENT_DISCLOSURE_VERSION = "sms_consent_v1_2026_07"
SMS_CONSENT_CHECKBOX_TEXT = (
    "I agree to receive conversational SMS messages from TopAI RE Tools regarding my "
    "real estate inquiry, including requested property information, responses to questions, "
    "appointment scheduling, reminders, and follow-up. Message frequency varies. Message and "
    "data rates may apply. Reply STOP to opt out or HELP for help. Consent is not a condition "
    "of purchasing goods or services."
)

SMS_SUPPORT_DISPLAY = "(720) 903-2519"
SMS_SUPPORT_E164 = "+17209032519"
OPERATOR_LEGAL_NAME = "Sky Blue Holdings LLC"


def validate_sms_consent_form(form) -> tuple[dict | None, str | None]:
    name = str(form.get("name") or "").strip()[:120]
    phone_raw = str(form.get("phone") or form.get("phone_number") or "").strip()
    message = str(form.get("message") or "").strip()[:2000]
    # Checkbox is optional and must not be required.
    sms_consent = str(form.get("sms_consent") or "").lower() in {"1", "true", "on", "yes"}

    if not name:
        return None, "Enter your name."
    phone, phone_error = validate_e164_phone(phone_raw)
    if phone_error or not phone:
        return None, "Enter a valid mobile phone number with area code."
    if not message:
        return None, "Enter your real estate inquiry or message."

    return {
        "name": name,
        "phone_number": phone,
        "message": message,
        "sms_consent": sms_consent,
    }, None


def create_sms_consent_inquiry(
    *,
    name: str,
    phone_number: str,
    message: str,
    sms_consent: bool,
    source_url: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> int:
    """Persist a public inquiry. Never sends SMS."""
    now = datetime.now(timezone.utc).isoformat()
    consent_at = now if sms_consent else None
    disclosure_version = SMS_CONSENT_DISCLOSURE_VERSION if sms_consent else SMS_CONSENT_DISCLOSURE_VERSION
    # IP / UA retained for consent audit and form abuse prevention (TCPA/A2P records).
    return db.create_sms_consent_inquiry(
        name=name,
        phone_number=phone_number,
        message=message,
        sms_consent=bool(sms_consent),
        consent_at=consent_at,
        source_url=(source_url or "")[:500] or None,
        disclosure_version=disclosure_version,
        ip_address=(ip_address or "")[:64] or None,
        user_agent=(user_agent or "")[:500] or None,
        created_at=now,
    )


def outbound_sms_blocked_for_phone(phone_number: str) -> str | None:
    """
    Return an error message if outbound SMS must be blocked for this number.
    Blocks when the latest public inquiry for the number has sms_consent=false.
    """
    phone, err = validate_e164_phone(phone_number)
    if err or not phone:
        return None
    row = db.latest_sms_consent_inquiry_for_phone(phone)
    if not row:
        return None
    if not row.get("sms_consent"):
        return (
            "Outbound SMS is blocked for this number: the public inquiry was submitted "
            "without SMS consent."
        )
    return None


def phone_has_affirmative_sms_consent(phone_number: str) -> bool:
    phone, err = validate_e164_phone(phone_number)
    if err or not phone:
        return False
    row = db.latest_sms_consent_inquiry_for_phone(phone)
    return bool(row and row.get("sms_consent"))
