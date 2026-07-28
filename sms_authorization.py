"""Centralized SMS send authorization for every outbound path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
import db
import tenant_sms_db as tdb
from sms_consent import outbound_sms_blocked_for_phone
from sms_validation import validate_e164_phone

BLOCKED_EXTERNAL_MSG = (
    "SMS cannot be sent because consent has not been verified for this externally sourced lead."
)
BLOCKED_GENERIC_MSG = "SMS cannot be sent for this lead due to consent or compliance restrictions."
NO_SENDER_MSG = (
    "SMS is not activated for this account. Assign and verify a sender number before sending."
)
NO_CERT_MSG = (
    "SMS cannot be sent until you certify that this contact consented to receive messages "
    "from you or your business."
)
QUIET_HOURS_MSG = "SMS cannot be sent during quiet hours for this account."
RATE_LIMIT_MSG = "SMS rate limit reached for this account. Try again later."

USER_CERTIFIED = "user_certified"
VERIFIED_LEGACY = "verified"
OPTED_OUT = "opted_out"
REVOKED = "revoked"
NOT_PERMITTED = "not_permitted"
SUPPRESSED = "suppressed"
INVALID = "invalid_number"
NOT_CERTIFIED = "not_certified"
UNVERIFIED_LEGACY = "unverified"

ONE_TO_ONE_CERT_TEXT = (
    "I confirm that this contact has consented to receive SMS messages from me or my business "
    "for this purpose. I maintain the supporting consent records and am responsible for TCPA, "
    "DNC, carrier, brokerage, privacy, opt-out, and local-law compliance."
)

CAMPAIGN_CERT_TEXT = (
    "I certify that every selected recipient has provided valid consent to receive these SMS "
    "messages from me or my business for the stated campaign purpose. I maintain the supporting "
    "consent records and can provide them if required. I understand that uploading or possessing "
    "a phone number alone does not establish consent, and I am responsible for TCPA, DNC, carrier, "
    "brokerage, privacy, opt-out, and local-law compliance."
)


def _is_blocked_flag(value):
    if value in (0, 1, "0", "1", True, False):
        return bool(int(value)) if value in (0, 1, "0", "1") else bool(value)
    return bool(value)


def _status(lead):
    return (lead.get("sms_consent_status") or NOT_CERTIFIED).strip().lower()


def _certified_status(status):
    return status in {USER_CERTIFIED, VERIFIED_LEGACY}


def get_tenant_from_number(account_phone):
    return tdb.get_sender_by_number(account_phone)


def require_tenant_sender(user_id):
    sender = tdb.get_active_sender(user_id)
    if sender:
        return sender, None
    # Legacy Twilio: platform From number is allowed without per-tenant row.
    if (config.SMS_PROVIDER or "").lower() == "twilio":
        from sms_provider import TwilioSmsProvider

        legacy = TwilioSmsProvider()
        if legacy.is_configured():
            return {
                "user_id": user_id,
                "sender_number": config.TWILIO_PHONE_NUMBER,
                "sms_provider": "twilio",
                "sms_enabled": True,
                "registration_status": "verified",
            }, None
    # Never use global SIMPLETEXTING_PHONE_NUMBER as implicit tenant sender.
    return None, NO_SENDER_MSG


def _provider_credentials_ok():
    provider = (config.SMS_PROVIDER or "").lower()
    if provider == "simpletexting":
        return bool(config.SIMPLETEXTING_API_TOKEN)
    if provider == "twilio":
        return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN)
    return False


def _in_quiet_hours(user_id):
    # Approximate using UTC hour vs configured quiet window (tenant TZ refinement later).
    hour = datetime.now(timezone.utc).hour
    start = config.SMS_QUIET_HOURS_START
    end = config.SMS_QUIET_HOURS_END
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def _rate_limited(user_id, phone_number):
    now = datetime.now(timezone.utc)
    if tdb.count_sends_since(user_id, (now - timedelta(minutes=1)).isoformat()) >= config.SMS_MSGS_PER_MINUTE:
        return True
    if tdb.count_sends_since(user_id, (now - timedelta(hours=1)).isoformat()) >= config.SMS_MSGS_PER_HOUR:
        return True
    if tdb.count_sends_since(user_id, (now - timedelta(days=1)).isoformat()) >= config.SMS_MSGS_PER_DAY:
        return True
    if (
        tdb.count_sends_to_contact_since(
            user_id, phone_number, (now - timedelta(days=1)).isoformat()
        )
        >= config.SMS_MAX_PER_CONTACT_PER_DAY
    ):
        return True
    return False


def can_send_sms(
    tenant_id,
    contact_id,
    *,
    user_id=None,
    campaign_id=None,
    message_purpose=None,
    message_body=None,
    require_attestation_record=True,
    skip_quiet_hours=False,
):
    """
    Return (ok: bool, message: str).
    tenant_id is TopAI users.id. user_id defaults to tenant_id (same-account model).
    """
    user_id = user_id or tenant_id
    if not tenant_id or not contact_id:
        return False, BLOCKED_GENERIC_MSG

    sender, sender_err = require_tenant_sender(tenant_id)
    if sender_err:
        return False, sender_err

    if not _provider_credentials_ok():
        return False, "SMS provider is not configured."

    lead = db.get_lead(contact_id, tenant_id)
    if not lead:
        return False, "Lead not found for this account."

    phone = (lead.get("phone_number") or "").strip()
    cleaned, err = validate_e164_phone(phone)
    if err or not cleaned:
        return False, "Lead does not have a valid mobile phone number."

    status = _status(lead)
    if (lead.get("opt_out_status") or "active") == "opted_out" or status == OPTED_OUT:
        return False, "This lead opted out. Do not send SMS."
    if status in {REVOKED, NOT_PERMITTED, SUPPRESSED}:
        return False, BLOCKED_GENERIC_MSG
    if status == INVALID:
        return False, "This phone number is marked invalid."

    if tdb.is_suppressed(tenant_id, cleaned):
        return False, "This number is on the suppression list."

    inquiry_block = outbound_sms_blocked_for_phone(cleaned)
    if inquiry_block:
        return False, inquiry_block

    if not skip_quiet_hours and _in_quiet_hours(tenant_id):
        return False, QUIET_HOURS_MSG

    if _rate_limited(tenant_id, cleaned):
        return False, RATE_LIMIT_MSG

    if campaign_id:
        campaign = tdb.get_campaign(campaign_id, tenant_id)
        if not campaign:
            return False, "Campaign not found."
        if campaign.get("status") not in {"processing", "scheduled"}:
            return False, "Campaign is not in a sendable state."
        att = tdb.get_valid_campaign_attestation(
            tenant_id,
            campaign_id,
            message_body=campaign.get("message_template") or message_body or "",
            audience_snapshot_id=campaign.get("audience_snapshot_id") or "",
            purpose=campaign.get("campaign_purpose") or message_purpose or "campaign",
        )
        if not att:
            return False, "Campaign certification is missing or outdated. Recertify before sending."
        recipients = tdb.list_campaign_recipients(campaign_id, tenant_id, eligible_only=True)
        if not any(
            r.get("lead_id") == contact_id or r.get("phone_number") == cleaned for r in recipients
        ):
            return False, "Contact is not in the certified campaign audience."
        return True, ""

    if require_attestation_record and message_body is not None:
        att = tdb.latest_attestation_for_lead(
            tenant_id,
            contact_id,
            message_body=message_body,
            purpose=message_purpose or "real_estate_follow_up",
        )
        if not att:
            if lead.get("external_source_id") or str(lead.get("source") or "").startswith("external:"):
                return False, BLOCKED_EXTERNAL_MSG
            return False, NO_CERT_MSG
    elif not _certified_status(status):
        if lead.get("external_source_id") or str(lead.get("source") or "").startswith("external:"):
            return False, BLOCKED_EXTERNAL_MSG
        if require_attestation_record:
            return False, NO_CERT_MSG

    return True, ""


def record_one_to_one_attestation(
    user_id,
    lead_id,
    *,
    message_body,
    source_page,
    message_purpose="real_estate_follow_up",
    actor_user_id=None,
):
    """Persist certification and mark lead user_certified (not TopAI-verified)."""
    import external_leads_db as xdb

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    if (lead.get("opt_out_status") or "") == "opted_out" or _status(lead) == OPTED_OUT:
        return None, "Opted-out leads cannot be certified for SMS."

    provider = (config.SMS_PROVIDER or "simpletexting").lower()
    attestation_id = tdb.create_subscriber_attestation(
        user_id,
        actor_user_id or user_id,
        lead_id,
        message_purpose=message_purpose,
        message_body=message_body,
        source_page=source_page,
        provider=provider,
    )
    xdb.set_lead_sms_consent_state(
        lead_id,
        user_id,
        sms_consent_status=USER_CERTIFIED,
        sms_sending_blocked=False,
        actor_user_id=actor_user_id or user_id,
        source="subscriber_attestation",
        metadata={"attestation_id": attestation_id},
    )
    tdb.append_sms_audit(
        user_id,
        "consent_certification_accepted",
        actor_user_id=actor_user_id or user_id,
        lead_id=lead_id,
        new_value=USER_CERTIFIED,
        metadata={"attestation_id": attestation_id, "source_page": source_page},
    )
    return attestation_id, None


def attest_internal_sms_consent(
    user_id, lead_id, *, agent_name=None, message_body="", source_page="ai_sms"
):
    body = message_body or f"internal:{lead_id}"
    _att_id, err = record_one_to_one_attestation(
        user_id,
        lead_id,
        message_body=body,
        source_page=source_page,
        message_purpose="real_estate_follow_up",
    )
    if err:
        return False, err
    return can_send_sms(
        user_id,
        lead_id,
        message_purpose="real_estate_follow_up",
        message_body=body,
    )
