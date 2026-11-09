"""D5 follow-up: silent-default coercions removed from write paths.

Regression tests for the merge-time fixes applied on top of the Wave 1
remediation branches:

- ``ask_topai.schemas._clean_int`` no longer digit-strips garbage into a
  wrong id/number ("abc123" used to become 123 — a different lead).
- ``sanitize_create_task`` returns an error for an invalid priority
  instead of silently substituting "normal".
- ``sanitize_create_lead`` drops an unrecognized lead_type instead of
  inventing "buyer" (except "buy"-containing text, which still maps).
- ``crm_db.set_lead_status`` rejects an invalid status instead of
  silently coercing it to "new" on a write.
"""

import db
import crm_db
from ask_topai.schemas import (
    _clean_int,
    sanitize_create_lead,
    sanitize_create_task,
)


def _make_lead(user_id):
    from lead_service import upsert_crm_lead

    import uuid

    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    phone = f"+1303{n:07d}"[:12]
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Coercion Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    return lead_id


# --- _clean_int: strict, no digit-stripping ---------------------------------

def test_clean_int_rejects_digit_stripping():
    assert _clean_int("abc123") is None
    assert _clean_int("12x") is None
    assert _clean_int("1.5") is None


def test_clean_int_accepts_real_ints():
    assert _clean_int(42) == 42
    assert _clean_int("42") == 42
    assert _clean_int("  7 ") == 7
    assert _clean_int(3.0) == 3
    assert _clean_int(None) is None
    assert _clean_int("") is None
    assert _clean_int(True) is None
    assert _clean_int(1.5) is None


# --- sanitize_create_task: invalid priority is an error ----------------------

def test_create_task_invalid_priority_is_error():
    cleaned, err = sanitize_create_task(
        {"title": "Call back", "priority": "banana", "due_at": "tomorrow"},
        "Call back tomorrow",
    )
    assert err == "Invalid task priority."


def test_create_task_valid_priority_ok():
    cleaned, err = sanitize_create_task(
        {"title": "Call back", "priority": "high", "due_at": "tomorrow"},
        "Call back tomorrow",
    )
    assert err is None
    assert cleaned["priority"] == "high"


# --- sanitize_create_lead: unrecognized lead_type dropped, not invented ------

def test_create_lead_unrecognized_type_dropped():
    cleaned, err = sanitize_create_lead(
        {"name": "Pat", "phone": "+13035550199", "lead_type": "flipper"},
        "Pat the flipper called from 303-555-0199 about a house",
    )
    assert err is None
    assert cleaned.get("lead_type") in (None, "")


def test_create_lead_buy_text_still_maps_to_buyer():
    cleaned, err = sanitize_create_lead(
        {"name": "Pat", "phone": "+13035550199", "lead_type": "homebuyer"},
        "Pat the homebuyer called from 303-555-0199",
    )
    assert err is None
    assert cleaned["lead_type"] == "buyer"


# --- set_lead_status: invalid status rejected, not coerced to "new" ----------

def test_set_lead_status_rejects_invalid(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    lead, error = crm_db.set_lead_status(u1, lead_id, "bogus_status")
    assert lead is None
    assert error == "Invalid lead status."
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
    assert row["status"] == "new"


def test_set_lead_status_accepts_valid_and_alias(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    lead, error = crm_db.set_lead_status(u1, lead_id, "qualified")
    assert error is None
    assert lead["status"] == "qualified"
    lead, error = crm_db.set_lead_status(u1, lead_id, "hot")
    assert error is None
    assert lead["status"] == "qualified"
