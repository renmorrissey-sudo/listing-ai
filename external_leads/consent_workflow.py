"""Consent confirmation / evidence workflows for externally sourced leads."""

from __future__ import annotations

import os
import secrets
import uuid

import config
import crm_db
import external_leads_db as xdb
from crm_constants import (
    CONSENT_CONFIRMATION_STATEMENT,
    CONSENT_METHODS,
    EVIDENCE_TYPES,
    VERBAL_CONSENT_SCRIPT,
)
from sms_validation import validate_e164_phone

ALLOWED_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".txt"}
ALLOWED_UPLOAD_MIMES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/plain",
}


def save_evidence_upload(user_id, file_storage):
    """Save upload privately. Returns (ref, error)."""
    if not file_storage or not getattr(file_storage, "filename", None):
        return None, None
    filename = file_storage.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return None, "Unsupported evidence file type."
    data = file_storage.read(config.CONSENT_UPLOAD_MAX_BYTES + 1)
    if len(data) > config.CONSENT_UPLOAD_MAX_BYTES:
        return None, "Evidence file exceeds size limit."
    mime = getattr(file_storage, "mimetype", None) or ""
    if mime and mime not in ALLOWED_UPLOAD_MIMES:
        return None, "Unsupported evidence content type."
    os.makedirs(config.CONSENT_UPLOAD_DIR, exist_ok=True)
    token = f"{user_id}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(config.CONSENT_UPLOAD_DIR, token)
    with open(path, "wb") as fh:
        fh.write(data)
    return token, None


def resolve_upload_path(user_id, upload_ref):
    if not upload_ref or "/" in upload_ref or "\\" in upload_ref or ".." in upload_ref:
        return None
    if not str(upload_ref).startswith(f"{user_id}_"):
        return None
    path = os.path.join(config.CONSENT_UPLOAD_DIR, upload_ref)
    if not os.path.isfile(path):
        return None
    return path


def confirm_qualifying_consent(user_id, lead_id, form, *, file_storage=None):
    """
    Structured verification. Requires attestation checkbox and evidence details.
    External-platform method requires platform-specific fields.
    """
    from db import get_lead

    lead = get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    if (lead.get("opt_out_status") or "") == "opted_out" or (
        lead.get("sms_consent_status") or ""
    ) == "opted_out":
        return None, "Opted-out leads cannot be re-enabled through consent confirmation."

    method = (form.get("consent_method") or "").strip()
    if method not in CONSENT_METHODS:
        return None, "Select a valid consent method."
    if not form.get("attestation_accepted"):
        return None, "You must affirm the confirmation statement."

    consent_at = (form.get("consent_at") or "").strip()
    if not consent_at:
        return None, "Consent date/time is required."
    agent = (form.get("authorized_agent_name") or "").strip()
    brokerage = (form.get("authorized_brokerage_name") or "").strip()
    if not agent or not brokerage:
        return None, "Authorized agent and brokerage are required."
    phone, phone_err = validate_e164_phone(form.get("phone_number") or lead.get("phone_number"))
    if phone_err or not phone:
        return None, phone_err or "Valid mobile number is required."
    if phone != (lead.get("phone_number") or ""):
        return None, "Consent phone number must match the lead phone number."
    purpose = (form.get("communication_purpose") or "").strip() or "real_estate_follow_up"
    evidence_type = (form.get("evidence_type") or "").strip()
    if evidence_type not in EVIDENCE_TYPES:
        return None, "Select a valid evidence type."
    disclosure = (form.get("disclosure_text") or "").strip()
    source_provider = (form.get("source_provider") or form.get("consent_source") or "").strip()
    source_url = (form.get("source_url") or "").strip()
    notes = (form.get("notes") or "").strip()

    if method == "external_platform":
        if not source_provider:
            return None, "Platform/source name is required for external-platform consent."
        how = (form.get("platform_authorization_explanation") or "").strip()
        if not how:
            return None, "Explain how the disclosure authorized the identified sender or brokerage."
        if not (source_url or disclosure or file_storage):
            return None, "Provide a source URL, disclosure text, or uploaded evidence for external-platform consent."
        notes = (notes + "\n" + how).strip() if notes else how

    if method == "verbal":
        required_verbal = [
            ("verbal_context", "Verbal consent context/location is required."),
            ("verbal_response", "Consumer affirmative response is required."),
        ]
        for key, msg in required_verbal:
            if not (form.get(key) or "").strip():
                return None, msg
        if not disclosure:
            disclosure = VERBAL_CONSENT_SCRIPT
        notes = (
            f"Context: {form.get('verbal_context')}\n"
            f"Response: {form.get('verbal_response')}\n"
            f"{notes}"
        ).strip()

    if not disclosure and evidence_type == "disclosure_text":
        return None, "Disclosure text is required."

    upload_ref, upload_err = save_evidence_upload(user_id, file_storage)
    if upload_err:
        return None, upload_err

    evidence_id = xdb.create_consent_evidence(
        user_id,
        lead_id,
        {
            "consent_status": "confirmed",
            "consent_method": method,
            "source_provider": source_provider,
            "source_url": source_url,
            "consent_at": consent_at,
            "authorized_agent_name": agent,
            "authorized_brokerage_name": brokerage,
            "phone_number": phone,
            "communication_purpose": purpose,
            "disclosure_text": disclosure,
            "disclosure_version": "agent_confirmed_v1",
            "evidence_type": evidence_type,
            "upload_ref": upload_ref,
            "notes": notes,
            "confirmed_by_user_id": user_id,
            "confirmed_at": None,  # set via mark
            "attestation_accepted": True,
            "audit_json": {
                "attestation": CONSENT_CONFIRMATION_STATEMENT,
                "method": method,
            },
        },
    )
    xdb.mark_evidence_confirmed(evidence_id, user_id, user_id)
    xdb.set_lead_sms_consent_state(
        lead_id,
        user_id,
        sms_consent_status="user_certified",
        sms_sending_blocked=False,
        actor_user_id=user_id,
        source="consent_confirm",
        metadata={"evidence_id": evidence_id},
    )
    xdb.append_consent_audit(
        user_id,
        lead_id,
        actor_user_id=user_id,
        action="consent_user_certified",
        previous_value=lead.get("sms_consent_status"),
        new_value="user_certified",
        source="consent_confirm",
        metadata={"evidence_id": evidence_id},
    )
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "consent_user_certified",
        "Subscriber certified SMS consent with supporting record (not TopAI verified)",
        {"evidence_id": evidence_id, "method": method},
    )
    try:
        crm_db.resolve_needs_attention_by_reason(
            user_id,
            lead_id,
            "consent_review_required",
            resolution_reason="Subscriber certified consent",
        )
    except Exception:
        pass
    return {"evidence_id": evidence_id, "lead_id": lead_id}, None
