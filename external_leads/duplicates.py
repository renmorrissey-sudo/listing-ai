"""Tenant-scoped duplicate detection for external leads."""

from __future__ import annotations

import db
import external_leads_db as xdb
from lead_service import normalize_phone_e164


def find_duplicate(user_id, *, phone=None, email=None, external_source_id=None, external_record_id=None):
    """
    Return (lead_dict_or_None, match_reason).
    Prefer external_source + record_id, then phone, then email.
    Never matches across tenants.
    """
    if external_source_id and external_record_id:
        lead = xdb.find_lead_by_external_record(user_id, external_source_id, external_record_id)
        if lead:
            return lead, "external_record_id"

    if phone:
        normalized = normalize_phone_e164(phone)
        if normalized:
            lead = db.get_lead_by_phone(user_id, normalized)
            if lead:
                return lead, "phone"

    if email:
        lead = xdb.find_lead_by_email(user_id, email)
        if lead:
            return lead, "email"

    return None, None
