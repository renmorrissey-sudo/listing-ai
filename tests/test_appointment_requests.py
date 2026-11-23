"""Phase III-4 appointment requests through the gate — tests.

Red-before-green limbs (fail on un-fixed code, pass on the gate):
(1) the auto-execute removal — BOTH branches (exact +/-900s and
vague-window slots[0]); (2) the confirm-time collision refusal;
(3) the voice status-claim removal.

Every test asserts on durable ROWS (appointments,
harness_appointment_requests, harness_approvals, harness_audit, lead
timeline, mock provider sends) — never on dashboard counters.

Setup follows the harness-test precedent: mock SMS transport, a
verified tenant sender + accepted SMS terms where a send is under
test, and the gate inputs pinned open unless the test is about that
input. The calendar is the mock provider (keyed to the resolved mock
transport); no real calendar is ever connected.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import db
import scheduling
from calendar_providers import mock as mock_calendar
from harness.appointment_requests import (
    build_booking_confirmation_text,
    create_confirm_approval,
    create_request,
    get_request,
    list_pending_requests,
    maybe_mark_lead_confirmed,
)
from harness.approvals import approve_approval, execute_approved, reject_approval
from harness.validation import validate_appointment_request_draft
from sms_providers.mock import clear_sent_messages, sent_messages
from tests.test_follow_up_sms_sending import _enable_sending, _make_lead
from tests.test_harness_staging_safety import (
    _GateOpen,
    _set_settings,
    _staging_mock_config,
)

DENVER = ZoneInfo("America/Denver")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_appointment_requests")
        conn.execute("DELETE FROM harness_approvals")
    clear_sent_messages()
    mock_calendar.clear()
    yield
    clear_sent_messages()
    mock_calendar.clear()


def _next_weekday_slot(hour=14, minute=0, days_ahead=1):
    """Next weekday (Mon-Fri) at hour:minute Denver time, as UTC ISO.

    Tomorrow at 2pm Denver is inside business hours, so the slot is
    findable by the availability search; the weekday guard keeps the
    suite green on weekends.
    """
    now_denver = datetime.now(timezone.utc).astimezone(DENVER)
    day = now_denver + timedelta(days=days_ahead)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot.astimezone(timezone.utc).isoformat()


def _slot_window(hour=14, duration_minutes=30):
    start = _next_weekday_slot(hour=hour)
    start_dt = datetime.fromisoformat(start)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    return start, end_dt.isoformat()


def _weekday_name(iso):
    return datetime.fromisoformat(iso).astimezone(DENVER).strftime("%A")


def _appointments(user_id, lead_id=None):
    with db.get_db() as conn:
        if lead_id is not None:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE user_id = ? AND lead_id = ?",
                (user_id, lead_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE user_id = ?", (user_id,)
            ).fetchall()
    return [dict(r) for r in rows]


def _requests(user_id, lead_id=None):
    with db.get_db() as conn:
        if lead_id is not None:
            rows = conn.execute(
                "SELECT * FROM harness_appointment_requests "
                "WHERE user_id = ? AND lead_id = ?",
                (user_id, lead_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM harness_appointment_requests WHERE user_id = ?",
                (user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def _approvals(user_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_approvals WHERE user_id = ?", (user_id,)
            ).fetchall()
        ]


def _audit_rows(user_id, action=None):
    with db.get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_audit WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        ]
    if action:
        rows = [r for r in rows if r["action"] == action]
    return rows


def _timeline_rows(user_id, lead_id, event_type=None):
    with db.get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM lead_activities WHERE user_id = ? AND lead_id = ? "
                "ORDER BY id",
                (user_id, lead_id),
            ).fetchall()
        ]
    if event_type:
        rows = [r for r in rows if r["event_type"] == event_type]
    return rows


def _enable_mock_calendar(user_id):
    import calendar_connections_db as cal_db

    return cal_db.ensure_mock_connection(user_id)


def _make_request_direct(user_id, lead_id, slot_start, slot_end, source_ref_id=1):
    """A pending request row + its confirm approval, bypassing the
    availability search (collision tests need a request for a busy
    slot). Returns (request, approval_id)."""
    request, action = create_request(
        user_id,
        lead_id,
        channel="sms",
        source_ref_type="sms",
        source_ref_id=source_ref_id,
        raw_request_text="Can we meet tomorrow at 2pm?",
        requested_start_at=slot_start,
        requested_end_at=slot_end,
        provenance={"kind": "exact", "raw_text": "tomorrow at 2pm"},
        appointment_type="phone_call",
        timezone_name="America/Denver",
    )
    assert action == "created", action
    assert request is not None
    return request, request.get("approval_id")


def _approve_and_execute(user_id, approval_id):
    approval, error = approve_approval(user_id, approval_id)
    assert error is None, error
    assert approval["status"] == "approved", approval
    return execute_approved(user_id, approval_id)


# ---------------------------------------------------------------------------
# (1) Auto-execute removal — BOTH branches
# ---------------------------------------------------------------------------


def test_exact_slot_inbound_never_books(two_users, monkeypatch):
    """Exact slot match (+/-900s) used to auto-book via
    ``_book_or_reschedule``. Now it creates a pending request and no
    appointment — in any mode."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    day = _weekday_name(slot_start)
    analysis = {
        "appointment_requested": True,
        "intent": "book_appointment",
        "appointment_details": {
            "text": f"Can we meet {day.lower()} at 2pm?",
            "time": "2pm",
            "day": day.lower(),
        },
    }
    for mode in ("drafts_only", "supervised", "authorized"):
        _set_settings(u1, mode=mode)
        result = scheduling.create_appointment_request(
            u1, db.get_lead(lead_id, u1), analysis,
            source_ref_type="sms", source_ref_id=100 + len(_requests(u1)),
            raw_request_text=f"Can we meet {day.lower()} at 2pm?",
        )
        assert result["action"] == "requested", result
        assert _appointments(u1, lead_id) == []
        assert "I've scheduled it" not in (result.get("message") or "")
        assert "holding" in (result.get("message") or "").lower(), result
    requests = _requests(u1, lead_id)
    assert len(requests) == 1, requests
    assert requests[0]["status"] == "requested"
    assert requests[0]["requested_start_at"] == slot_start


def test_vague_window_inbound_never_books_slots0(two_users, monkeypatch):
    """A vague window ("tomorrow afternoon") used to auto-book the
    FIRST open slot (``slots[0]``). Now it creates a pending request
    holding the window start — the agent picks the exact slot."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, _slot_end = _slot_window(hour=12)
    day = _weekday_name(slot_start)
    analysis = {
        "appointment_requested": True,
        "intent": "book_appointment",
        "appointment_details": {
            "text": f"Can we meet {day.lower()} afternoon?",
            "day": day.lower(),
        },
    }
    result = scheduling.create_appointment_request(
        u1, db.get_lead(lead_id, u1), analysis,
        source_ref_type="sms", source_ref_id=201,
        raw_request_text=f"Can we meet {day.lower()} afternoon?",
    )
    assert result["action"] == "requested", result
    assert _appointments(u1, lead_id) == []
    request = _requests(u1, lead_id)[0]
    assert request["status"] == "requested"
    # Window kind holds the window start (noon); the exact slot is
    # picked at confirm time, never booked here.
    assert request["requested_start_at"] == slot_start
    assert json.loads(request["requested_window_json"])["kind"] == "window"


def test_missing_time_request_has_null_slot_and_asks_for_time(two_users, monkeypatch):
    """Ambiguous / missing time -> a pending request with a NULL slot;
    the template asks for a time instead of guessing one."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    analysis = {
        "appointment_requested": True,
        "intent": "book_appointment",
        "appointment_details": {"text": "Can we meet sometime soon?"},
    }
    result = scheduling.create_appointment_request(
        u1, db.get_lead(lead_id, u1), analysis,
        source_ref_type="sms", source_ref_id=301,
        raw_request_text="Can we meet sometime soon?",
    )
    assert result["action"] == "requested", result
    assert _appointments(u1, lead_id) == []
    request = _requests(u1, lead_id)[0]
    assert request["requested_start_at"] is None
    assert "what time works best" in (result.get("message") or "").lower(), result


def test_past_slot_flagged_and_never_rolled_forward(two_users, monkeypatch):
    """A past slot is flagged in_past and kept as parsed — the parser
    never silently rolls it forward — and the approval gate refuses
    it (slot_in_past)."""
    from scheduling import parse_requested_window_with_provenance

    u1, _ = two_users
    # An explicit past DATE (yesterday at 2pm): the parser must flag
    # it in_past and keep it as parsed — never silently roll it to
    # tomorrow. (A bare time with no day is ambiguous, not a past
    # slot; the parser reads it as the next occurrence.)
    yesterday = (datetime.now(timezone.utc).astimezone(DENVER) - timedelta(days=1)).strftime("%Y-%m-%d")
    details = {
        "text": f"Can we meet on {yesterday} at 2pm?",
        "date": yesterday,
        "time": "2pm",
    }
    after, _before, _exact, provenance = parse_requested_window_with_provenance(
        details, timezone_name="America/Denver"
    )
    assert after is not None
    assert provenance["in_past"] is True
    # The parsed slot is the past time itself, not tomorrow.
    assert datetime.fromisoformat(after) < datetime.now(timezone.utc)

    lead_id, _phone = _make_lead(u1)
    request, approval_id = _make_request_direct(
        u1, lead_id, after, _before, source_ref_id=401
    )
    _cleaned, error = validate_appointment_request_draft(
        u1,
        {
            "request_id": request["id"],
            "lead_id": lead_id,
            "slot_start_at": after,
            "confirmation_text": "You're booked.",
        },
    )
    assert error == "slot_in_past", error


def test_exact_hour_with_window_hint_parses_exact():
    """Phase III-4 fix: an exact hour parses exact regardless of the
    coach's window-hint field. Observed staging shape — details
    {"day":"wednesday","time":"15:00","window":"afternoon","text":
    "Can we meet Wednesday at 3pm?"} — parsed as a 3-hour window
    (kind "window") because the exactness test ran over the whole
    details blob, which contains "afternoon" from the hint; confirm
    books the stored slot, so a straight Confirm would have booked a
    3-hour appointment."""
    from scheduling import parse_requested_window_with_provenance

    details = {
        "day": "wednesday",
        "time": "15:00",
        "window": "afternoon",
        "text": "Can we meet Wednesday at 3pm?",
    }
    after, before, exact, provenance = parse_requested_window_with_provenance(
        details, timezone_name="America/Denver"
    )
    assert after is not None and before is not None
    assert exact is True, provenance
    assert provenance["kind"] == "exact", provenance
    start = datetime.fromisoformat(after)
    end = datetime.fromisoformat(before)
    assert (end - start).total_seconds() == 30 * 60, (after, before)
    assert start.astimezone(DENVER).hour == 15
    assert start.astimezone(DENVER).minute == 0


def test_exact_hour_with_window_hint_request_holds_30min_slot(two_users, monkeypatch):
    """End to end for the same defect: the pending request stores the
    30-minute exact slot (what confirm would book), and the draft is
    the exact variant — no "(or thereabouts)"."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    day_dt = datetime.now(timezone.utc).astimezone(DENVER) + timedelta(days=2)
    while day_dt.weekday() >= 5:
        day_dt += timedelta(days=1)
    day = day_dt.strftime("%A")
    analysis = {
        "appointment_requested": True,
        "intent": "book_appointment",
        "appointment_details": {
            "day": day.lower(),
            "time": "15:00",
            "window": "afternoon",
            "text": f"Can we meet {day.lower()} at 3pm?",
        },
    }
    result = scheduling.create_appointment_request(
        u1, db.get_lead(lead_id, u1), analysis,
        source_ref_type="sms", source_ref_id=801,
        raw_request_text=f"Can we meet {day.lower()} at 3pm?",
    )
    assert result["action"] == "requested", result
    assert result["provenance"]["kind"] == "exact", result["provenance"]
    assert "or thereabouts" not in (result.get("message") or ""), result
    request = _requests(u1, lead_id)[0]
    start = datetime.fromisoformat(request["requested_start_at"])
    end = datetime.fromisoformat(request["requested_end_at"])
    assert (end - start).total_seconds() == 30 * 60, request
    assert start.astimezone(DENVER).hour == 15
    assert json.loads(request["requested_window_json"])["kind"] == "exact"


def test_genuinely_vague_around_still_parses_window():
    """Guard against over-fixing: the lead's own "around" still
    downgrades an hour to a window."""
    from scheduling import parse_requested_window_with_provenance

    details = {
        "text": "Can we meet tomorrow around 2pm?",
        "time": "2pm",
    }
    after, before, exact, provenance = parse_requested_window_with_provenance(
        details, timezone_name="America/Denver"
    )
    assert exact is False, provenance
    assert provenance["kind"] == "window", provenance
    start = datetime.fromisoformat(after)
    end = datetime.fromisoformat(before)
    assert (end - start).total_seconds() == 180 * 60, (after, before)


def test_genuinely_vague_afternoon_no_hour_still_parses_window():
    """Guard: an afternoon with no hour is still a window."""
    from scheduling import parse_requested_window_with_provenance

    details = {
        "text": "Can we meet tomorrow afternoon?",
        "window": "afternoon",
    }
    after, before, exact, provenance = parse_requested_window_with_provenance(
        details, timezone_name="America/Denver"
    )
    assert exact is False, provenance
    assert provenance["kind"] == "window", provenance


def test_booking_request_leaves_lead_status_unchanged(two_users, monkeypatch):
    """Phase III-4 fix (c2), request level: the live §7 step-5 inbound
    carried the coach's suggested status; request creation must leave
    the lead status where it was while the request is pending — only
    the confirm path writes appointment_scheduled."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    before = db.get_lead(lead_id, u1)["status"]
    result = scheduling.create_appointment_request(
        u1,
        db.get_lead(lead_id, u1),
        {
            "appointment_requested": True,
            "intent": "book_appointment",
            "suggested_lead_status": "appointment_scheduled",
            "appointment_details": {
                "day": "wednesday",
                "time": "15:00",
                "window": "afternoon",
                "text": "Can we meet Wednesday at 3pm?",
            },
        },
        source_ref_type="sms",
        source_ref_id=901,
        raw_request_text="Can we meet Wednesday at 3pm?",
    )
    assert result["action"] == "requested", result
    assert db.get_lead(lead_id, u1)["status"] == before


def test_old_book_branches_are_gone():
    """Source-level guard: the auto-book entry points must not exist
    anymore (a re-introduction fails the suite, not a live test)."""
    import pathlib

    source = pathlib.Path("scheduling.py").read_text()
    assert "def handle_inbound_scheduling" not in source
    assert "def _book_or_reschedule" not in source
    assert "I've scheduled it" not in source


def test_coach_prompt_no_longer_tells_model_to_book(two_users):
    """The coach prompt's appointment instruction no longer contains
    'book' — the model is told to hold the request for the agent."""
    import sms_prompts

    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    prompt = sms_prompts.build_inbound_reply_analysis_prompt(
        db.get_lead(lead_id, u1), [], "Can we meet tomorrow at 2pm?"
    )
    assert "book or offer nearby times" not in prompt
    assert "hold the request for the agent to confirm" in prompt
    assert "Never claim a booking" in prompt


# ---------------------------------------------------------------------------
# (3) Voice status-claim removal
# ---------------------------------------------------------------------------


def test_voice_webhook_never_sets_appointment_scheduled(two_users, monkeypatch):
    """A voice booking request creates the same pending request as SMS
    — it never sets lead status appointment_scheduled (no appointment
    exists) and never books."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    from lead_service import apply_voice_call_webhook_to_lead

    slot_start, _slot_end = _slot_window()
    day = _weekday_name(slot_start)
    normalized = {
        "status": "completed",
        "appointment_requested": True,
        "summary": f"Lead wants a showing {day.lower()} at 2pm",
        "transcript": f"Lead: can we meet {day.lower()} at 2pm? Agent: sure.",
        "outcome": "completed",
    }
    result = apply_voice_call_webhook_to_lead(
        u1, {"id": 501, "lead_id": lead_id}, normalized
    )
    assert result is not None
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] != "appointment_scheduled", lead
    assert _appointments(u1, lead_id) == []
    requests = _requests(u1, lead_id)
    assert len(requests) == 1, requests
    assert requests[0]["channel"] == "voice"
    assert requests[0]["status"] == "requested"
    assert requests[0]["requested_start_at"] == slot_start


# ---------------------------------------------------------------------------
# Never-confirmed / no-calendar / lead-confirmed
# ---------------------------------------------------------------------------


def test_never_confirmed_request_never_becomes_event(two_users, monkeypatch):
    """A request the agent never confirms never becomes an event and
    never tells the lead it is booked — no appointment, no SMS."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=601
    )
    assert approval_id is not None
    assert sent_messages() == []
    assert _appointments(u1, lead_id) == []
    assert get_request(u1, request["id"])["status"] == "requested"


def test_no_calendar_request_stays_pending_and_visible(two_users, monkeypatch):
    """No calendar connected: the request stays pending and visible,
    and the confirm refuses without booking (no_calendar)."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=701
    )
    assert request["no_calendar_connected"] == 1
    assert list_pending_requests(u1, lead_id) != []
    with _GateOpen():
        result = _approve_and_execute(u1, approval_id)
    # A settled refusal: ok=True (never retried), decision BLOCK.
    assert result["ok"] is True, result
    assert result["decision"] == "BLOCK", result
    assert result.get("retryable") is False, result
    assert "no_calendar" in (result.get("reason") or ""), result
    assert _appointments(u1, lead_id) == []
    assert sent_messages() == []
    # The request is back to pending — visible for a later confirm
    # once a calendar is connected.
    assert get_request(u1, request["id"])["status"] == "requested"
    assert list_pending_requests(u1, lead_id) != []


def test_lead_confirmed_does_not_create_event(two_users, monkeypatch):
    """The lead's 'yes' marks lead_confirmed — it does not create an
    event and sends nothing."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, _approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=801
    )
    marked = maybe_mark_lead_confirmed(u1, lead_id, "Yes, that works for me!")
    assert marked is not None
    assert marked["status"] == "lead_confirmed"
    assert _appointments(u1, lead_id) == []
    assert sent_messages() == []
    assert get_request(u1, request["id"])["status"] == "lead_confirmed"


# ---------------------------------------------------------------------------
# (2) Confirm-time collision refusal — three sources
# ---------------------------------------------------------------------------


def _confirm_setup(two_users, monkeypatch):
    """Mock calendar + sending + open gate; returns (u1, lead_id)."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized")
    assert _enable_mock_calendar(u1) is not None
    return u1


def test_confirm_collision_internal_appointment(two_users, monkeypatch):
    """Source (a): an internal appointment at the slot refuses the
    confirm — audit + timeline evidence, no second appointment."""
    u1 = _confirm_setup(two_users, monkeypatch)
    lead_a, _ = _make_lead(u1)
    lead_b, _ = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    appt, err, _alts = scheduling.create_calendar_event(
        u1,
        {
            "lead_id": lead_a,
            "start_at": slot_start,
            "end_at": slot_end,
            "appointment_type": "phone_call",
        },
    )
    assert err is None, err
    request, approval_id = _make_request_direct(
        u1, lead_b, slot_start, slot_end, source_ref_id=901
    )
    with _GateOpen():
        result = _approve_and_execute(u1, approval_id)
    # A settled refusal: ok=True (never retried), decision BLOCK.
    assert result["ok"] is True, result
    assert result["decision"] == "BLOCK", result
    assert result.get("retryable") is False, result
    assert "collision" in (result.get("reason") or "").lower(), result
    assert _appointments(u1, lead_b) == []
    assert len(_appointments(u1)) == 1
    assert get_request(u1, request["id"])["status"] == "conflicted"
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    assert row["status"] == "blocked"
    refused = _audit_rows(u1, action="appointment_booking_refused")
    assert len(refused) == 1
    assert len(refused[0]["gate_reason"]) <= 500
    assert "internal" in refused[0]["gate_reason"]
    timeline = _timeline_rows(u1, lead_b, "appointment_booking_refused")
    assert len(timeline) == 1
    assert sent_messages() == []


def test_confirm_collision_second_pending_request(two_users, monkeypatch):
    """Source (b): two pending requests for the same slot collide with
    each other — neither can confirm while the other is pending. The
    agent declines one, then re-confirms the other, which books."""
    u1 = _confirm_setup(two_users, monkeypatch)
    lead_a, _ = _make_lead(u1)
    lead_b, _ = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    req_a, appr_a = _make_request_direct(
        u1, lead_a, slot_start, slot_end, source_ref_id=1001
    )
    req_b, appr_b = _make_request_direct(
        u1, lead_b, slot_start, slot_end, source_ref_id=1002
    )
    with _GateOpen():
        first = _approve_and_execute(u1, appr_a)
    assert first["ok"] is True, first
    assert first["decision"] == "BLOCK", first
    assert "collision" in (first.get("reason") or "").lower(), first
    assert "request" in (first.get("reason") or "").lower(), first
    assert _appointments(u1, lead_a) == []
    assert get_request(u1, req_a["id"])["status"] == "conflicted"
    # Decline the competing request, then re-confirm the first.
    approval, error = reject_approval(u1, appr_b, "other request wins")
    assert error is None, error
    assert get_request(u1, req_b["id"])["status"] == "declined"
    approval_id2, error = create_confirm_approval(u1, req_a["id"])
    assert error is None, error
    with _GateOpen():
        second = _approve_and_execute(u1, approval_id2)
    assert second["ok"] is True, second
    assert len(_appointments(u1, lead_a)) == 1
    assert _appointments(u1, lead_b) == []
    assert get_request(u1, req_a["id"])["status"] == "confirmed"


def test_confirm_collision_external_mock_event(two_users, monkeypatch):
    """Source (c): a mock external calendar event at the slot refuses
    the confirm."""
    u1 = _confirm_setup(two_users, monkeypatch)
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    mock_calendar.seed_event(
        u1,
        {
            "summary": "Dentist",
            "start_at": slot_start,
            "end_at": slot_end,
        },
    )
    request, approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=1101
    )
    with _GateOpen():
        result = _approve_and_execute(u1, approval_id)
    assert result["ok"] is True, result
    assert result["decision"] == "BLOCK", result
    assert "collision" in (result.get("reason") or "").lower(), result
    refused = _audit_rows(u1, action="appointment_booking_refused")
    assert refused and "external" in refused[0]["gate_reason"]
    assert _appointments(u1, lead_id) == []
    assert get_request(u1, request["id"])["status"] == "conflicted"


def test_confirm_external_read_failure_is_retryable(two_users, monkeypatch):
    """An external calendar that cannot be read REFUSES the confirm —
    retryable, approval back to approved, request back to requested,
    nothing booked."""
    u1 = _confirm_setup(two_users, monkeypatch)
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=1201
    )
    with _GateOpen(), patch.object(
        mock_calendar.MockCalendarProvider,
        "list_events",
        side_effect=Exception("calendar down"),
    ):
        approval, error = approve_approval(u1, approval_id)
        assert error is None, error
        result = execute_approved(u1, approval_id)
    assert result["ok"] is False, result
    assert result.get("retryable") is True, result
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    assert row["status"] == "approved"
    assert get_request(u1, request["id"])["status"] == "requested"
    assert _appointments(u1, lead_id) == []


# ---------------------------------------------------------------------------
# Confirm success + evidence
# ---------------------------------------------------------------------------


def test_confirm_success_books_texts_and_audits(two_users, monkeypatch):
    """Happy path: approve + execute books the internal appointment,
    writes the mock external event, sets the lead status, sends the
    exact confirmation text, and leaves audit + timeline evidence."""
    u1 = _confirm_setup(two_users, monkeypatch)
    lead_id, phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=1301
    )
    with _GateOpen():
        result = _approve_and_execute(u1, approval_id)
    assert result["ok"] is True, result
    assert result.get("appointment_id") is not None
    assert result.get("external_event_id") is not None
    assert result.get("message_id") is not None

    appts = _appointments(u1, lead_id)
    assert len(appts) == 1
    assert appts[0]["start_at"] == slot_start
    assert db.get_lead(lead_id, u1)["status"] == "appointment_scheduled"

    external = mock_calendar.list_events_for(u1)
    assert len(external) == 1
    assert external[0]["id"] == result["external_event_id"]
    assert external[0]["start_at"] == slot_start

    recorded = sent_messages()
    assert len(recorded) == 1, recorded
    assert recorded[0]["to"] == phone
    expected_text = build_booking_confirmation_text(slot_start, "America/Denver")
    assert recorded[0]["body"] == expected_text
    assert "You're booked for" in recorded[0]["body"]

    confirmed = get_request(u1, request["id"])
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_appointment_id"] == result["appointment_id"]
    assert confirmed["external_event_id"] == result["external_event_id"]
    assert confirmed["confirm_text_message_id"] == result["message_id"]

    assert _audit_rows(u1, action="appointment_request_confirmed") != []
    assert _audit_rows(u1, action="appointment_booking_created") != []
    assert _audit_rows(u1, action="approval_executed") != []
    assert _timeline_rows(u1, lead_id, "appointment_confirmed") != []
    for row in _audit_rows(u1):
        assert len(row["gate_reason"] or "") <= 500, row

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    assert row["status"] == "executed"


def test_decline_request_declines_without_text(two_users, monkeypatch):
    """Rejecting the approval declines the request — no appointment,
    no text to the lead."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=1401
    )
    approval, error = reject_approval(u1, approval_id, "not a fit")
    assert error is None, error
    assert approval["status"] == "rejected"
    assert get_request(u1, request["id"])["status"] == "declined"
    assert _appointments(u1, lead_id) == []
    assert sent_messages() == []
    assert _audit_rows(u1, action="appointment_request_declined") != []


def test_duplicate_request_attaches_without_second_row_or_text(two_users, monkeypatch):
    """A second inbound for the same tenant+lead+slot attaches to the
    existing request: one timeline entry, no second row, no second
    approval, no second text."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, _approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=1501
    )
    again, action = create_request(
        u1,
        lead_id,
        channel="sms",
        source_ref_type="sms",
        source_ref_id=1502,
        raw_request_text="Can we meet tomorrow at 2pm?",
        requested_start_at=slot_start,
        requested_end_at=slot_end,
        provenance={"kind": "exact"},
        timezone_name="America/Denver",
    )
    assert action == "duplicate", action
    assert again["id"] == request["id"]
    assert len(_requests(u1, lead_id)) == 1
    assert len(_approvals(u1)) == 1
    assert len(_timeline_rows(u1, lead_id, "appointment_request_duplicate")) == 1
    assert _audit_rows(u1, action="appointment_request_duplicate") != []
    assert sent_messages() == []


def test_cross_tenant_isolation(two_users, monkeypatch):
    """Another tenant cannot see, approve, or validate the request —
    404 semantics, never revealed."""
    _staging_mock_config(monkeypatch)
    u1, u2 = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=1601
    )
    assert get_request(u2, request["id"]) is None
    approval, error = approve_approval(u2, approval_id)
    assert approval is None and error == "not_found"
    _cleaned, error = validate_appointment_request_draft(
        u2,
        {
            "request_id": request["id"],
            "lead_id": lead_id,
            "slot_start_at": slot_start,
            "confirmation_text": "You're booked.",
        },
    )
    assert error == "request_not_found", error


def test_reconfirm_creates_fresh_approval_with_new_slot(two_users, monkeypatch):
    """A conflicted request can be re-confirmed with a new slot on the
    same row — the override is recorded, a fresh approval is created."""
    u1 = _confirm_setup(two_users, monkeypatch)
    lead_a, _ = _make_lead(u1)
    lead_b, _ = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    appt, err, _alts = scheduling.create_calendar_event(
        u1,
        {"lead_id": lead_a, "start_at": slot_start, "end_at": slot_end,
         "appointment_type": "phone_call"},
    )
    assert err is None, err
    request, approval_id = _make_request_direct(
        u1, lead_b, slot_start, slot_end, source_ref_id=1701
    )
    with _GateOpen():
        result = _approve_and_execute(u1, approval_id)
    assert result["ok"] is True, result
    assert result["decision"] == "BLOCK", result
    assert get_request(u1, request["id"])["status"] == "conflicted"

    new_start, new_end = _slot_window(hour=15)
    approval_id2, error = create_confirm_approval(
        u1, request["id"], slot_start_at=new_start, slot_end_at=new_end
    )
    assert error is None, error
    assert approval_id2 != approval_id
    updated = get_request(u1, request["id"])
    assert updated["requested_start_at"] == new_start
    assert updated["status"] == "conflicted"
    with _GateOpen():
        result2 = _approve_and_execute(u1, approval_id2)
    assert result2["ok"] is True, result2
    assert len(_appointments(u1, lead_b)) == 1


def test_no_calendar_connected_flag_set_at_creation(two_users, monkeypatch):
    """The no_calendar_connected flag is set at creation when no
    calendar is connected — the inbox hides Confirm until one is."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    request, _approval_id = _make_request_direct(
        u1, lead_id, slot_start, slot_end, source_ref_id=1801
    )
    assert request["no_calendar_connected"] == 1
    _enable_mock_calendar(u1)
    other_start, other_end = _slot_window(hour=15)
    request2, _a2 = _make_request_direct(
        u1, lead_id, other_start, other_end, source_ref_id=1802
    )
    assert request2["id"] != request["id"]
    assert request2["no_calendar_connected"] == 0


# ---------------------------------------------------------------------------
# Worker-level inbound paths (stubbed coach, real job + worker)
# ---------------------------------------------------------------------------


def _appointment_analysis(day, draft_reply, **overrides):
    analysis = {
        "summary": "Lead asked to meet.",
        "intent": "book_appointment",
        "draft_reply": draft_reply,
        "suggested_reply": draft_reply,
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "suggested_lead_status": "appointment_scheduled",
        "appointment_requested": True,
        "appointment_details": {
            "text": f"Can we meet {day.lower()} at 2pm?",
            "time": "2pm",
            "day": day.lower(),
        },
    }
    analysis.update(overrides)
    return analysis


def _make_inbound(user_id, lead_id, phone, body):
    return db.create_sms_message(
        user_id, None, "telnyx",
        {"phone_number": phone, "message_body": body},
        status="received", lead_id=lead_id, direction="inbound",
    )


def _run_auto_reply_job(user_id, lead_id, inbound_id, body):
    from harness.jobs import enqueue_job
    from workers.harness_worker import run_once

    job_id = enqueue_job(
        user_id,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": body,
            "receiving_number": "+18880000000",
        },
        idempotency_key=f"appt-e2e-{uuid.uuid4().hex[:12]}",
    )
    assert run_once() is True, "worker ran nothing for the enqueued job"
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    raw = dict(row).get("result_json")
    return json.loads(raw) if raw else {}


def test_handler_drafts_only_inbound_creates_request_no_appointment(two_users, monkeypatch):
    """Drafts Only inbound booking request: the handler creates the
    pending request (its creation point for this mode — the
    process_inbound_ai path never runs), drafts the deterministic
    template (never the model's false 'I've scheduled it' text),
    strips the appointment_scheduled suggestion, and sends nothing."""
    from harness.appointment_requests import build_confirmation_request_text

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    day = _weekday_name(slot_start)
    body = f"Can we meet {day.lower()} at 2pm?"
    inbound_id = _make_inbound(u1, lead_id, phone, body)
    coach_analysis = _appointment_analysis(
        day, "Yes, I've scheduled it for tomorrow at 2pm!"
    )
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": coach_analysis, "insight_id": None, "error": None},
    ):
        result = _run_auto_reply_job(u1, lead_id, inbound_id, body)
    assert result.get("decision") == "DRAFT", result
    assert result.get("sent") is False, result
    assert result.get("approval_id") is not None
    assert _appointments(u1, lead_id) == []
    assert sent_messages() == []
    requests = _requests(u1, lead_id)
    assert len(requests) == 1, requests
    assert requests[0]["status"] == "requested"
    assert requests[0]["requested_start_at"] == slot_start
    approvals = _approvals(u1)
    sms_approvals = [a for a in approvals if a["kind"] == "sms_reply"]
    assert len(sms_approvals) == 1, approvals
    draft = json.loads(sms_approvals[0]["draft_json"])
    expected = build_confirmation_request_text(
        slot_start, slot_end, "America/Denver", "exact"
    )
    assert draft["draft_reply"] == expected, draft
    assert "I've scheduled it" not in draft["draft_reply"]
    assert draft.get("suggested_lead_status") is None


def test_handler_authorized_inbound_sends_template_no_appointment(two_users, monkeypatch):
    """Authorized inbound booking request: the confirmation-request
    template auto-sends (one mock send), the request is created, and
    still no appointment exists and the lead is never told it is
    booked."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized")
    lead_id, phone = _make_lead(u1)
    slot_start, slot_end = _slot_window()
    day = _weekday_name(slot_start)
    body = f"Can we meet {day.lower()} at 2pm?"
    inbound_id = _make_inbound(u1, lead_id, phone, body)
    coach_analysis = _appointment_analysis(
        day, "Yes, I've scheduled it for tomorrow at 2pm!"
    )
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": coach_analysis, "insight_id": None, "error": None},
    ):
        result = _run_auto_reply_job(u1, lead_id, inbound_id, body)
    assert result.get("decision") == "ALLOW", result
    assert result.get("sent") is True, result
    assert _appointments(u1, lead_id) == []
    assert db.get_lead(lead_id, u1)["status"] != "appointment_scheduled"
    recorded = sent_messages()
    assert len(recorded) == 1, recorded
    assert recorded[0]["to"] == phone
    assert "holding" in recorded[0]["body"].lower(), recorded
    assert "I've scheduled it" not in recorded[0]["body"]
    requests = _requests(u1, lead_id)
    assert len(requests) == 1, requests
    assert requests[0]["status"] == "requested"
