"""Core ingest: always unverified + SMS blocked for external leads."""

from __future__ import annotations

import json
import logging

import crm_db
import external_leads_db as xdb
from external_leads.duplicates import find_duplicate
from lead_service import normalize_phone_e164
from sms_validation import validate_e164_phone

logger = logging.getLogger(__name__)


def ingest_external_lead(
    user_id,
    payload: dict,
    *,
    source_row: dict | None = None,
    method: str = "manual",
    import_batch_id=None,
    actor_user_id=None,
):
    """
    Create or update an externally sourced lead.
    Always results in sms_consent_status=unverified and sms_sending_blocked=true
    for *new* leads. Updates never upgrade consent or clear opt-out.
    """
    result = {
        "action": None,
        "lead_id": None,
        "duplicate_match": None,
        "pending_evidence_id": None,
        "error": None,
    }

    name = (payload.get("name") or payload.get("full_name") or "").strip()
    if not name:
        first = (payload.get("first_name") or "").strip()
        last = (payload.get("last_name") or "").strip()
        name = f"{first} {last}".strip() or "External Lead"

    phone_raw = payload.get("phone") or payload.get("phone_number") or ""
    phone, phone_err = validate_e164_phone(phone_raw)
    if phone_err or not phone:
        # Allow email-only external leads only if phone provided is empty but we still need phone for SMS uniqueness.
        # Schema requires phone_number NOT NULL — reject without valid phone.
        result["error"] = phone_err or "A valid mobile phone number is required."
        return result

    email = (payload.get("email") or "").strip()[:200] or None
    external_record_id = (payload.get("external_record_id") or payload.get("source_record_id") or "").strip()[:200] or None
    source_id = source_row["id"] if source_row else payload.get("external_source_id")
    source_name = (source_row or {}).get("name") or payload.get("source") or "external"
    source_label = f"external:{source_name}"
    notes_bits = []
    if payload.get("inquiry_notes") or payload.get("notes"):
        notes_bits.append(str(payload.get("inquiry_notes") or payload.get("notes")))
    if payload.get("property_address"):
        notes_bits.append(f"Property: {payload['property_address']}")
    if payload.get("property_url"):
        notes_bits.append(f"URL: {payload['property_url']}")
    notes = "\n".join(notes_bits)[:2000] or None

    existing, match = find_duplicate(
        user_id,
        phone=phone,
        email=email,
        external_source_id=source_id,
        external_record_id=external_record_id,
    )

    meta = {
        "ingest_method": method,
        "original_consent_status": payload.get("original_consent_status"),
        "original_consent_date": payload.get("original_consent_date"),
        "original_consent_text": payload.get("original_consent_text"),
        "raw_keys": sorted([k for k in payload.keys() if k != "raw_payload"]),
    }
    if payload.get("raw_payload") is not None:
        # Keep a safe truncated copy
        raw = payload.get("raw_payload")
        try:
            raw_s = json.dumps(raw) if not isinstance(raw, str) else raw
        except (TypeError, ValueError):
            raw_s = str(raw)
        meta["raw_payload_preview"] = raw_s[:4000]

    pond_status = (source_row or {}).get("default_pond_status") or payload.get("pond_status") or "claimable"
    lead_type = payload.get("lead_type") or (source_row or {}).get("default_lead_type")
    status = payload.get("status") or (source_row or {}).get("default_lead_status") or "new"

    if existing:
        # Never overwrite verified consent or clear opted_out via import
        result["duplicate_match"] = match
        result["lead_id"] = existing["id"]
        result["action"] = "updated"
        if (existing.get("sms_consent_status") or "") == "opted_out" or (
            existing.get("opt_out_status") or ""
        ) == "opted_out":
            # Append activity only; do not change consent
            result["action"] = "skipped_opted_out"
        else:
            xdb.update_external_lead_fields(
                existing["id"],
                user_id,
                name=name,
                email=email,
                notes=notes,
                lead_type=lead_type,
                property_interest=payload.get("property_interest") or payload.get("property_address"),
                external_payload_meta=meta,
                external_record_id=external_record_id or existing.get("external_record_id"),
                import_batch_id=import_batch_id,
            )
            # Ensure still blocked if somehow not — external updates never verify
            if (existing.get("sms_consent_status") or "") not in {"verified", "opted_out", "revoked", "not_permitted"}:
                xdb.set_lead_sms_consent_state(
                    existing["id"],
                    user_id,
                    sms_consent_status="unverified",
                    sms_sending_blocked=True,
                    actor_user_id=actor_user_id or user_id,
                    source=method,
                    metadata={"reason": "external_ingest_duplicate"},
                )
        crm_db.add_lead_activity(
            existing["id"],
            user_id,
            "external_lead_duplicate_matched",
            f"External lead matched by {match}",
            {"method": method, "match": match, "source": source_label},
            actor_user_id=actor_user_id or user_id,
        )
        xdb.append_consent_audit(
            user_id,
            existing["id"],
            actor_user_id=actor_user_id or user_id,
            action="duplicate_matched",
            previous_value=None,
            new_value=match,
            source=method,
            metadata={"source": source_label},
        )
    else:
        lead_id = xdb.create_external_lead(
            user_id,
            phone_number=phone,
            name=name,
            email=email,
            lead_type=lead_type,
            status=status,
            source=source_label,
            notes=notes,
            external_source_id=source_id,
            external_record_id=external_record_id,
            external_payload_meta=meta,
            pond_status=pond_status,
            import_batch_id=import_batch_id,
            property_interest=payload.get("property_interest") or payload.get("property_address"),
        )
        result["lead_id"] = lead_id
        result["action"] = "created"
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "external_lead_imported",
            f"External lead received via {method}",
            {"method": method, "source": source_label, "external_record_id": external_record_id},
            actor_user_id=actor_user_id or user_id,
        )
        xdb.append_consent_audit(
            user_id,
            lead_id,
            actor_user_id=actor_user_id or user_id,
            action="external_lead_imported",
            new_value="unverified",
            source=method,
            metadata={"source": source_label},
        )
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            "consent_review_required",
            priority="high",
            source_ref_type="external_lead",
            source_ref_id=lead_id,
        )

    lead_id = result["lead_id"]
    # Imported consent claims become pending evidence only — never verify.
    claimed_consent = str(payload.get("original_consent_status") or "").strip().lower()
    if claimed_consent in {"true", "yes", "1", "confirmed", "verified", "opted_in"}:
        evidence_id = xdb.create_consent_evidence(
            user_id,
            lead_id,
            {
                "consent_status": "pending",
                "consent_method": "external_platform",
                "source_provider": source_name,
                "source_record_id": external_record_id,
                "consent_at": payload.get("original_consent_date"),
                "phone_number": phone,
                "communication_purpose": "real_estate_follow_up",
                "disclosure_text": payload.get("original_consent_text"),
                "evidence_type": "platform_metadata",
                "notes": "Imported consent claim — requires agent review before SMS is enabled.",
                "audit_json": {"imported_claim": True, "method": method},
            },
        )
        result["pending_evidence_id"] = evidence_id
        xdb.append_consent_audit(
            user_id,
            lead_id,
            actor_user_id=actor_user_id or user_id,
            action="consent_evidence_added",
            new_value="pending",
            source=method,
            metadata={"evidence_id": evidence_id, "imported_claim": True},
        )

    logger.info(
        "external_ingest user=%s lead=%s action=%s method=%s",
        user_id,
        lead_id,
        result["action"],
        method,
    )
    return result
