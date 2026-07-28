"""Centralized SMS send authorization. Every outbound path must call can_send_sms."""

from __future__ import annotations

import db
import external_leads_db as xdb
from sms_consent import outbound_sms_blocked_for_phone
from sms_validation import validate_e164_phone

BLOCKED_EXTERNAL_MSG = (
    "SMS cannot be sent because consent has not been verified for this externally sourced lead."
)
BLOCKED_GENERIC_MSG = "SMS cannot be sent for this lead due to consent or compliance restrictions."

VERIFIED = "verified"
OPTED_OUT = "opted_out"
REVOKED = "revoked"
NOT_PERMITTED = "not_permitted"
UNVERIFIED = "unverified"


def _is_blocked_flag(value):
    if value in (0, 1, "0", "1", True, False):
        return bool(int(value)) if value in (0, 1, "0", "1") else bool(value)
    return bool(value)


def _is_external_lead(lead):
    return bool(
        lead.get("external_source_id")
        or str(lead.get("source") or "").startswith("external:")
    )


def can_send_sms(user_id, lead_id, *, purpose="real_estate_follow_up"):
    """
    Return (ok: bool, message: str).
    ok is True only when the lead may receive outbound SMS from this tenant.
    """
    if not user_id or not lead_id:
        return False, BLOCKED_GENERIC_MSG

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return False, "Lead not found for this account."

    phone = (lead.get("phone_number") or "").strip()
    cleaned, err = validate_e164_phone(phone)
    if err or not cleaned:
        return False, "Lead does not have a valid mobile phone number."

    status = (lead.get("sms_consent_status") or UNVERIFIED).strip().lower()
    sending_blocked = _is_blocked_flag(lead.get("sms_sending_blocked"))
    if lead.get("sms_sending_blocked") is None and status == UNVERIFIED:
        sending_blocked = True

    if (lead.get("opt_out_status") or "active") == "opted_out" or status == OPTED_OUT:
        return False, "This lead opted out. Do not send SMS."

    if status in {REVOKED, NOT_PERMITTED}:
        return False, BLOCKED_GENERIC_MSG

    if status != VERIFIED or sending_blocked:
        if _is_external_lead(lead):
            return False, BLOCKED_EXTERNAL_MSG
        return False, BLOCKED_GENERIC_MSG

    inquiry_block = outbound_sms_blocked_for_phone(cleaned)
    if inquiry_block:
        return False, inquiry_block

    return True, ""


def attest_internal_sms_consent(user_id, lead_id, *, agent_name=None):
    """
    For non-external leads only: record agent send-time attestation and mark verified.
    External leads must use the structured consent confirmation UI.
    """
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return False, "Lead not found."
    if _is_external_lead(lead):
        return can_send_sms(user_id, lead_id)
    if (lead.get("sms_consent_status") or "") == VERIFIED and not _is_blocked_flag(
        lead.get("sms_sending_blocked")
    ):
        return True, ""
    if (lead.get("opt_out_status") or "") == "opted_out":
        return False, "This lead opted out. Do not send SMS."

    evidence_id = xdb.create_consent_evidence(
        user_id,
        lead_id,
        {
            "consent_status": "confirmed",
            "consent_method": "direct_web_form",
            "source_provider": "topai_sms_assistant",
            "phone_number": lead.get("phone_number"),
            "communication_purpose": "real_estate_follow_up",
            "disclosure_text": (
                "Agent confirmed lead consented to SMS before send in AI SMS Assistant."
            ),
            "disclosure_version": "internal_send_attestation_v1",
            "evidence_type": "verbal_attestation",
            "authorized_agent_name": agent_name or "Subscriber agent",
            "authorized_brokerage_name": "TopAI subscriber account",
            "attestation_accepted": True,
            "confirmed_by_user_id": user_id,
            "notes": "Internal tool attestation — not used for externally sourced leads.",
        },
    )
    xdb.mark_evidence_confirmed(evidence_id, user_id, user_id)
    xdb.set_lead_sms_consent_state(
        lead_id,
        user_id,
        sms_consent_status=VERIFIED,
        sms_sending_blocked=False,
        actor_user_id=user_id,
        source="internal_send_attestation",
        metadata={"evidence_id": evidence_id},
    )
    return can_send_sms(user_id, lead_id)
