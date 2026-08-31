"""Shared lead contact update rules for CRM UI and voice tools."""

from __future__ import annotations

import re

import crm_db
import db
from lead_service import normalize_phone_e164


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTACT_FIELDS = {
    "name",
    "phone_number",
    "email",
    "lead_type",
    "property_interest",
    "notes",
    "next_action",
}


def _is_provided(data, *keys):
    return any(key in data for key in keys)


def _clean_optional(data, key, limit):
    if key not in data:
        return db._UNSET
    return str(data.get(key) or "").strip()[:limit]


def _changed_fields(before, after):
    changed = []
    for field in CONTACT_FIELDS:
        if (before.get(field) or "") != (after.get(field) or ""):
            changed.append(field)
    return changed


def update_lead_contact_info(user_id, lead_id, data, *, actor_user_id=None, source="crm"):
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found.", 404
    data = data or {}

    name = _clean_optional(data, "name", 200)
    if name is not db._UNSET and not name:
        return None, "Lead name is required.", 400

    phone_number = db._UNSET
    if _is_provided(data, "phone_number", "phone"):
        raw_phone = data.get("phone_number") if "phone_number" in data else data.get("phone")
        phone_number = normalize_phone_e164(raw_phone)
        digits = "".join(c for c in phone_number if c.isdigit())
        if len(digits) < 10:
            return None, "Enter a valid phone number.", 400
        duplicate = db.find_lead_by_phone_normalized(user_id, phone_number)
        if duplicate and int(duplicate["id"]) != int(lead_id):
            return None, f"That phone number already belongs to {duplicate.get('name') or 'another lead'}.", 409

    email = _clean_optional(data, "email", 200)
    if email is not db._UNSET and email and not EMAIL_RE.match(email):
        return None, "Enter a valid email address.", 400

    update = {
        "name": name,
        "phone_number": phone_number,
        "email": email,
        "lead_type": _clean_optional(data, "lead_type", 80),
        "property_interest": _clean_optional(data, "property_interest", 500),
        "notes": _clean_optional(data, "notes", 1500),
        "next_action": _clean_optional(data, "next_action", 500),
    }
    if all(value is db._UNSET for value in update.values()):
        return None, "Tell me which contact field to update.", 400

    db.update_lead_contact_info(lead_id, user_id, **update)
    updated = db.get_lead(lead_id, user_id)
    changed = _changed_fields(lead, updated)
    if changed:
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "contact_updated",
            "Lead contact info updated",
            {
                "fields": changed,
                "source": source,
                "previous": {field: lead.get(field) for field in changed},
                "current": {field: updated.get(field) for field in changed},
            },
            actor_user_id=actor_user_id or user_id,
        )
    return updated, None, 200
