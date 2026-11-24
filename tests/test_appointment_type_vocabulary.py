"""Phase III-5 vocabulary fix — appointment_type is phone_call / in_person / video_call.

Red-before-green: the coach's free-form type hint ("meet", "in-person",
"video call" ...) is mapped to the three-value vocabulary before the
APPOINTMENT_TYPES check, the coach prompt names the vocabulary, and a
"meet in person" inbound stores in_person (not the phone_call fallback).

Every test fails on un-fixed code: the prompt has no vocabulary, the
mapping does not exist, and "meet" falls back to phone_call.
"""

import pytest

import scheduling
from crm_constants import APPOINTMENT_TYPES


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("meet", "in_person"),
        ("meeting", "in_person"),
        ("in person", "in_person"),
        ("in-person", "in_person"),
        ("inperson", "in_person"),
        ("meet in person", "in_person"),
        ("Meet In Person", "in_person"),
        ("phone", "phone_call"),
        ("call", "phone_call"),
        ("phone call", "phone_call"),
        ("Phone Call", "phone_call"),
        ("video", "video_call"),
        ("video call", "video_call"),
        ("zoom", "video_call"),
        ("virtual", "video_call"),
        ("carrier pigeon", "phone_call"),
        ("", "phone_call"),
        (None, "phone_call"),
    ],
)
def test_appointment_type_mapping_table(candidate, expected):
    """Each free-form synonym maps to its mode; unknown falls back."""
    details = {"type": candidate} if candidate is not None else {}
    assert scheduling._appointment_type_from_details(details, None) == expected


def test_appointment_type_mapping_keeps_kinds():
    """A kind already in APPOINTMENT_TYPES is preserved, not flattened
    to phone_call — the mapping runs before the set check, it does not
    replace it."""
    assert (
        scheduling._appointment_type_from_details(
            {"type": "buyer_consultation"}, None
        )
        == "buyer_consultation"
    )
    assert (
        scheduling._appointment_type_from_details(
            {"type": "property_showing"}, None
        )
        == "property_showing"
    )


def test_appointment_type_vocabulary_contains_modes():
    """in_person and video_call are in APPOINTMENT_TYPES (9 values)."""
    assert "in_person" in APPOINTMENT_TYPES
    assert "video_call" in APPOINTMENT_TYPES
    assert "phone_call" in APPOINTMENT_TYPES
    assert len(APPOINTMENT_TYPES) == 9


def test_coach_prompt_constrains_appointment_type_vocabulary():
    """The appointment instruction names the three-value vocabulary —
    the coach is constrained to modes, not free-form type hints."""
    import db
    from sms_prompts import build_inbound_reply_analysis_prompt

    lead = {"name": "Test", "phone_number": "+13035550199", "lead_type": "buyer"}
    prompt = build_inbound_reply_analysis_prompt(lead, [], "Can we meet Friday?")
    assert "phone_call" in prompt
    assert "in_person" in prompt
    assert "video_call" in prompt
    # The type hint is one of the three values — not open-ended.
    assert "one of phone_call, in_person, video_call" in prompt


def test_meet_in_person_request_stores_in_person(two_users, monkeypatch):
    """A 'meet in person' inbound stores in_person on the request,
    not the phone_call fallback."""
    import db

    from tests.test_appointment_requests import _slot_window, _weekday_name
    from tests.test_follow_up_sms_sending import _make_lead
    from tests.test_harness_staging_safety import (
        _set_settings,
        _staging_mock_config,
    )

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, _slot_end = _slot_window()
    day = _weekday_name(slot_start)
    _set_settings(u1, mode="drafts_only")
    analysis = {
        "appointment_requested": True,
        "intent": "book_appointment",
        "appointment_details": {
            "text": f"Can we meet in person {day.lower()} at 2pm?",
            "time": "2pm",
            "day": day.lower(),
            "type": "meet in person",
        },
    }
    result = scheduling.create_appointment_request(
        u1,
        db.get_lead(lead_id, u1),
        analysis,
        source_ref_type="sms",
        source_ref_id=901,
        raw_request_text=f"Can we meet in person {day.lower()} at 2pm?",
    )
    assert result["action"] == "requested", result
    request = result["request"]
    assert request is not None
    assert request["appointment_type"] == "in_person", request
