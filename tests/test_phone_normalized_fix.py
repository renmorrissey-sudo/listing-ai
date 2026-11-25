"""phone_normalized fix: inbound SMS routing can find external leads.

Root cause (III-6 close-out, "Pre-existing gap found during
verification"): external_leads_db.create_external_lead never stored
phone_normalized, and db.update_lead_contact_info never updated it on
a phone edit. The inbound routing query db.list_lead_owners_by_phone
matches ONLY on phone_normalized, so an external lead that texts in is
invisible to routing and gets duplicated in the wrong tenant (live
proof: the III-6 step-4 inject from Lead A's number created stray lead
22 in tenant 1 instead of matching Lead A, id 20).

Red-before-green: the positive tests fail on un-fixed code
(phone_normalized stays NULL / stale) and pass after the fix. The
no-phone-edit guard passes on un-fixed code by construction and is kept
as a regression guard.
"""

import uuid

import db
import external_leads_db as xdb
from external_leads.ingest import ingest_external_lead
from migrations.runner import apply_pending_migrations


def _unique_phone():
    """A unique, valid E.164 US number (+1 555 + 7 digits)."""
    return f"+1555{str(uuid.uuid4().int)[-7:]}"


def _external_lead(user_id, phone):
    """Create an external lead via the ingest path (the live form path)."""
    apply_pending_migrations()
    sid = xdb.create_external_lead_source(
        user_id,
        name="Portal A",
        category="portal_inquiry",
        provider_key="portal-a",
        import_method="manual",
        default_pond_status="claimable",
    )
    source = xdb.get_external_lead_source(sid, user_id)
    result = ingest_external_lead(
        user_id,
        {
            "full_name": "Ext Lead",
            "phone": phone,
            "external_record_id": f"ext-{uuid.uuid4().hex[:8]}",
        },
        source_row=source,
        method="manual",
    )
    assert result["error"] is None, result
    return result["lead_id"]


def test_external_lead_inbound_routing_finds_existing_lead(two_users):
    """An external lead that texts in routes to the existing lead.

    The webhook resolves the tenant by lead ownership
    (list_lead_owners_by_phone) and then resolves the lead
    (find_lead_by_phone_normalized). Both must find the external lead —
    otherwise routing fails and the webhook creates a duplicate lead in
    the wrong tenant (the III-6 step-4 live failure).
    """
    u1, _ = two_users
    phone = _unique_phone()
    lead_id = _external_lead(u1, phone)

    # The stored row must carry the normalized value.
    lead = db.get_lead(lead_id, u1)
    assert lead["phone_normalized"] == phone

    # The routing lookup (owners by phone) must find this tenant's lead.
    owners = db.list_lead_owners_by_phone(phone)
    assert len(owners) == 1
    assert owners[0]["user_id"] == u1
    assert owners[0]["lead_id"] == lead_id

    # The lead resolution after tenant resolution must return the same lead.
    found = db.find_lead_by_phone_normalized(u1, phone)
    assert found is not None
    assert found["id"] == lead_id


def test_external_lead_formatted_phone_normalized(two_users):
    """A formatted number at ingest is stored normalized and routable.

    The form accepts "(555) 123-4567" style input; validate_e164_phone
    cleans it to E.164. The stored phone_normalized must be the E.164
    value so the routing lookup finds the lead from either form.
    """
    u1, _ = two_users
    digits = str(uuid.uuid4().int)[-7:]
    formatted = f"(555) {digits[:3]}-{digits[3:]}"
    expected = f"+1555{digits}"
    lead_id = _external_lead(u1, formatted)

    lead = db.get_lead(lead_id, u1)
    assert lead["phone_number"] == expected
    assert lead["phone_normalized"] == expected

    owners = db.list_lead_owners_by_phone(expected)
    assert len(owners) == 1
    assert owners[0]["lead_id"] == lead_id
    # The formatted form must resolve to the same owner.
    owners_formatted = db.list_lead_owners_by_phone(formatted)
    assert len(owners_formatted) == 1
    assert owners_formatted[0]["lead_id"] == lead_id


def test_update_lead_contact_info_sets_phone_normalized(two_users):
    """Editing a lead's phone updates phone_normalized to the new value."""
    u1, _ = two_users
    apply_pending_migrations()
    old_phone = _unique_phone()
    lead_id = db.create_lead_record(u1, old_phone, name="Edit Lead", status="new")
    assert db.get_lead(lead_id, u1)["phone_normalized"] == old_phone

    new_digits = str(uuid.uuid4().int)[-7:]
    new_formatted = f"(555) {new_digits[:3]}-{new_digits[3:]}"
    new_expected = f"+1555{new_digits}"
    db.update_lead_contact_info(lead_id, u1, phone_number=new_formatted)

    lead = db.get_lead(lead_id, u1)
    assert lead["phone_number"] == new_formatted
    assert lead["phone_normalized"] == new_expected

    # The new number must now route to this lead; the old number must not.
    owners = db.list_lead_owners_by_phone(new_expected)
    assert len(owners) == 1
    assert owners[0]["lead_id"] == lead_id
    assert db.list_lead_owners_by_phone(old_phone) == []


def test_update_lead_contact_info_without_phone_leaves_normalized(two_users):
    """Editing a non-phone field must not touch phone_normalized.

    Regression guard: the phone_normalized update is gated on a phone
    being provided, so a name-only edit leaves the normalized value
    exactly as it was.
    """
    u1, _ = two_users
    apply_pending_migrations()
    phone = _unique_phone()
    lead_id = db.create_lead_record(u1, phone, name="Guard Lead", status="new")
    assert db.get_lead(lead_id, u1)["phone_normalized"] == phone

    db.update_lead_contact_info(lead_id, u1, name="Renamed Lead")

    lead = db.get_lead(lead_id, u1)
    assert lead["name"] == "Renamed Lead"
    assert lead["phone_number"] == phone
    assert lead["phone_normalized"] == phone
