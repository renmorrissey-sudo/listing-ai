"""Public SMS consent / real-estate inquiry form helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import config
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

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _bool_db_value(value: bool):
    """Postgres BOOLEAN rejects int 1/0 from psycopg; SQLite accepts bool/int."""
    flag = bool(value)
    if getattr(config, "DB_ENGINE", "sqlite") == "postgres":
        return flag
    return 1 if flag else 0


def validate_sms_consent_form(form) -> tuple[dict | None, str | None]:
    first_name = str(form.get("first_name") or "").strip()[:60]
    last_name = str(form.get("last_name") or "").strip()[:60]
    # Backward-compatible single name field
    legacy_name = str(form.get("name") or "").strip()[:120]
    if not first_name and not last_name and legacy_name:
        parts = legacy_name.split(None, 1)
        first_name = parts[0][:60]
        last_name = (parts[1] if len(parts) > 1 else "")[:60]
    name = f"{first_name} {last_name}".strip()[:120]

    phone_raw = str(form.get("phone") or form.get("phone_number") or "").strip()
    email = str(form.get("email") or "").strip()[:200] or None
    message = str(form.get("message") or "").strip()[:2000]
    campaign_source = str(
        form.get("campaign_source") or form.get("source") or form.get("utm_campaign") or ""
    ).strip()[:120] or None
    sms_consent = str(form.get("sms_consent") or "").lower() in {"1", "true", "on", "yes"}

    if not first_name:
        return None, "Enter your first name."
    if not last_name:
        return None, "Enter your last name."
    phone, phone_error = validate_e164_phone(phone_raw)
    if phone_error or not phone:
        return None, "Enter a valid mobile phone number with area code (for example +1XXXXXXXXXX)."
    if email and not _EMAIL_RE.match(email):
        return None, "Enter a valid email address, or leave the email field blank."
    if not message:
        return None, "Enter your real estate inquiry or message."
    if not sms_consent:
        return None, "Check the SMS consent box to confirm you agree to receive texts."

    return {
        "first_name": first_name,
        "last_name": last_name,
        "name": name,
        "phone_number": phone,
        "email": email,
        "message": message,
        "sms_consent": True,
        "campaign_source": campaign_source,
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
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    campaign_source: str | None = None,
) -> tuple[int, bool]:
    """
    Persist a public inquiry. Never sends SMS.
    Returns (inquiry_id, created_new).
    Dedupes affirmative consent for the same phone + disclosure version.
    """
    now = datetime.now(timezone.utc).isoformat()
    consent_at = now if sms_consent else None
    disclosure_version = SMS_CONSENT_DISCLOSURE_VERSION

    if sms_consent:
        existing = db.find_sms_consent_inquiry_duplicate(
            phone_number,
            disclosure_version=disclosure_version,
            require_consent=True,
        )
        if existing:
            db.touch_sms_consent_inquiry(
                existing["id"],
                name=name,
                first_name=first_name,
                last_name=last_name,
                email=email,
                message=message,
                source_url=source_url,
                campaign_source=campaign_source,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            return existing["id"], False

    inquiry_id = db.create_sms_consent_inquiry(
        name=name,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone_number=phone_number,
        message=message,
        sms_consent=bool(sms_consent),
        consent_at=consent_at,
        source_url=(source_url or "")[:500] or None,
        disclosure_version=disclosure_version,
        ip_address=(ip_address or "")[:64] or None,
        user_agent=(user_agent or "")[:500] or None,
        campaign_source=(campaign_source or "")[:120] or None,
        created_at=now,
    )
    return inquiry_id, True


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
