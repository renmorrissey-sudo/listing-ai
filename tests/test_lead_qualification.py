"""Phase III-2 lead qualification state machine + scoring tests.

Covers every PM acceptance criterion: the transition table (every
disallowed jump refused with no write and no audit row; legal
transitions write audit + timeline rows), scoring worked examples
per band with component breakdowns asserted, the qualified
threshold counter-example, the model suggestion path (renders,
changes nothing until confirmed), the unqualified rules + re-open,
score recompute on field changes and signal events, the section 2.5
regression (the model can no longer silently write ``qualified``),
the no-SMS guarantee, and the migration 046 postconditions.

All tests use synthetic data only; no provider calls are made.
Qualification ops must create no harness_jobs, sms_messages, or
harness_approvals rows (asserted directly).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import crm_db
import db
from harness import qualification as q

HOT_FIELDS = {
    "budget": "500k-1m",
    "timeline": "soon",
    "preapproval": "yes",
    "motivation": "relocating",
}  # 25 + 15 + 25 + 10 = 75, Hot, qualified

WARM_FIELDS = {
    "budget": "300-500k",
    "timeline": "soon",
    "preapproval": "in-progress",
    "motivation": "downsizing",
}  # 15 + 15 + 15 + 5 = 50, Warm, not qualified (score < 60)

COLD_FIELDS = {
    "budget": "under-300k",
    "timeline": "later",
    "preapproval": "no",
    "motivation": "other",
}  # 5 + 5 + 0 + 0 = 10, Cold, not qualified


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, status="new"):
    lead_id = db.create_lead_record(
        user_id,
        _unique_phone(),
        name="Qualification Lead",
        status=status,
        source="test",
    )
    return lead_id


def _lead(user_id, lead_id):
    return db.get_lead(lead_id, user_id)


def _set_status_direct(user_id, lead_id, status):
    """Set a lead status bypassing the qualification guard (test setup
    only — mirrors how a lead reaches a lane state before III-2)."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET status = ? WHERE id = ? AND user_id = ?",
            (status, lead_id, user_id),
        )


def _insert_sms(user_id, lead_id, direction, status, created_at):
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_messages
                (user_id, lead_id, provider, direction, phone_number,
                 message_body, status, created_at)
            VALUES (?, ?, 'test', ?, ?, 'test message', ?, ?)
            """,
            (user_id, lead_id, direction, _unique_phone(), status, created_at),
        )
        return cur.lastrowid


def _insert_voice_call(user_id, lead_id, direction, created_at,
                       appointment_requested=0):
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, lead_id, provider, direction, phone_number,
                 status, appointment_requested, created_at)
            VALUES (?, ?, 'test', ?, ?, 'queued', ?, ?)
            """,
            (user_id, lead_id, direction, _unique_phone(),
             appointment_requested, created_at),
        )
        return cur.lastrowid


def _audit_rows(user_id, action=None):
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM harness_audit WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    rows = [dict(r) for r in rows]
    if action:
        rows = [r for r in rows if r.get("action") == action]
    return rows


def _timeline_status_changes(user_id, lead_id):
    return [
        a for a in crm_db.list_lead_activities(user_id, lead_id)
        if a.get("event_type") == "status_change"
    ]


def _table_count(table):
    with db.get_db() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(dict(row)["c"])


def _iso(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


# ---------------------------------------------------------------------------
# Migration 046
# ---------------------------------------------------------------------------

def test_migration_046_columns_exist_with_defaults(two_users):
    """Migration 046 added the six columns; existing leads default
    to unknown fields and score 0."""
    u1, _ = two_users
    db.init_db()
    with db.get_db() as conn:
        cols = {dict(r)["name"] for r in conn.execute("PRAGMA table_info(leads)")}
        assert {
            "qualification_budget",
            "qualification_timeline",
            "qualification_preapproval",
            "qualification_motivation",
            "qualification_score",
            "qualification_score_json",
        } <= cols
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            ("046_lead_qualification",),
        ).fetchone()
        assert row is not None
    lead_id = _make_lead(u1)
    lead = _lead(u1, lead_id)
    assert lead["qualification_budget"] == "unknown"
    assert lead["qualification_timeline"] == "unknown"
    assert lead["qualification_preapproval"] == "unknown"
    assert lead["qualification_motivation"] == "unknown"
    assert lead["qualification_score"] == 0
    assert lead["qualification_score_json"] is None


def test_migration_046_postconditions_pass(two_users):
    """The migration's own inline postconditions pass on the test DB."""
    import importlib

    m046 = importlib.import_module("migrations.versions.046_lead_qualification")

    u1, _ = two_users
    db.init_db()
    with db.get_db() as conn:
        m046.verify_sqlite(conn)  # raises on a missing column


# ---------------------------------------------------------------------------
# Pure scorer
# ---------------------------------------------------------------------------

def test_score_hot_worked_example_breakdown():
    total, breakdown, band = q.score_lead(HOT_FIELDS, {})
    assert total == 75
    assert band == "Hot"
    assert breakdown["budget"] == {"value": "500k-1m", "points": 25}
    assert breakdown["timeline"] == {"value": "soon", "points": 15}
    assert breakdown["preapproval"] == {"value": "yes", "points": 25}
    assert breakdown["motivation"] == {"value": "relocating", "points": 10}
    assert breakdown["replied"] == {"value": False, "points": 0}
    assert breakdown["booked"] == {"value": False, "points": 0}


def test_score_warm_worked_example_breakdown():
    total, breakdown, band = q.score_lead(WARM_FIELDS, {})
    assert total == 50
    assert band == "Warm"
    assert breakdown["budget"] == {"value": "300-500k", "points": 15}
    assert breakdown["timeline"] == {"value": "soon", "points": 15}
    assert breakdown["preapproval"] == {"value": "in-progress", "points": 15}
    assert breakdown["motivation"] == {"value": "downsizing", "points": 5}


def test_score_cold_worked_example_breakdown():
    total, breakdown, band = q.score_lead(COLD_FIELDS, {})
    assert total == 10
    assert band == "Cold"
    assert breakdown["budget"] == {"value": "under-300k", "points": 5}
    assert breakdown["timeline"] == {"value": "later", "points": 5}
    assert breakdown["preapproval"] == {"value": "no", "points": 0}
    assert breakdown["motivation"] == {"value": "other", "points": 0}


def test_score_max_and_signals():
    total, breakdown, band = q.score_lead(
        {"budget": "over-1m", "timeline": "immediate", "preapproval": "yes",
         "motivation": "first-home"},
        {"replied": True, "booked": True},
    )
    assert total == 110
    assert band == "Hot"
    assert breakdown["budget"]["points"] == 30
    assert breakdown["timeline"]["points"] == 25
    assert breakdown["replied"]["points"] == 10
    assert breakdown["booked"]["points"] == 10


def test_score_unknown_fields_zero_not_negative():
    total, breakdown, band = q.score_lead({}, {})
    assert total == 0
    assert band == "Cold"
    assert all(item["points"] == 0 for item in breakdown.values())


def test_qualified_verdict_needs_all_three_conditions():
    qualified, reasons = q.qualified_verdict(HOT_FIELDS, 75)
    assert qualified is True
    assert any("75 >= 60" in r and "met" in r for r in reasons)
    qualified, reasons = q.qualified_verdict(
        {"budget": "over-1m", "timeline": "immediate", "preapproval": "no",
         "motivation": "relocating"},
        65,
    )
    assert qualified is False
    assert any("pre-approval no" in r and "not met" in r for r in reasons)


# ---------------------------------------------------------------------------
# Validation: never silently coerce
# ---------------------------------------------------------------------------

def test_invalid_field_value_refused_with_field_name(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, error = q.set_qualification_fields(
        u1, lead_id, {"budget": "millions"}, actor="user",
    )
    assert result is None
    assert error is not None and "Budget" in error
    lead = _lead(u1, lead_id)
    assert lead["qualification_budget"] == "unknown"
    assert lead["qualification_score"] == 0
    assert lead["status"] == "new"


def test_unknown_field_name_refused(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, error = q.set_qualification_fields(
        u1, lead_id, {"credit_score": "excellent"}, actor="user",
    )
    assert result is None
    assert error is not None and "credit_score" in error


def test_no_change_write_writes_nothing(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, error = q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert error is None
    assert result["transition"] == "qualified"
    before_audit = len(_audit_rows(u1, q.AUDIT_ACTION))
    before_timeline = len(_timeline_status_changes(u1, lead_id))
    result, error = q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert error is None
    assert result["changed"] == {}
    assert result["transition"] is None
    assert len(_audit_rows(u1, q.AUDIT_ACTION)) == before_audit
    assert len(_timeline_status_changes(u1, lead_id)) == before_timeline


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "current,target",
    [
        ("new", "qualified"),
        ("new", "unqualified"),
        ("new", "nurture"),
        ("contacted", "qualified"),
        ("contacted", "unqualified"),
        ("contacted", "nurture"),
        ("qualified", "nurture"),
        ("qualified", "unqualified"),
        ("qualified", "contacted"),
        ("qualified", "new"),
        ("unqualified", "qualified"),
        ("unqualified", "nurture"),
        ("unqualified", "contacted"),
        ("nurture", "qualified"),
        ("nurture", "unqualified"),
        ("nurture", "contacted"),
        ("qualifying", "new"),
        ("qualifying", "contacted"),
    ],
)
def test_disallowed_transitions_refused_no_write_no_audit(two_users, current, target):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    _set_status_direct(u1, lead_id, current)
    before_audit = len(_audit_rows(u1, q.AUDIT_ACTION))
    before_timeline = len(_timeline_status_changes(u1, lead_id))
    updated, error = q.request_status_change(u1, lead_id, target, actor="user")
    assert updated is None
    assert error is not None and "Illegal qualification transition" in error
    assert _lead(u1, lead_id)["status"] == current
    assert len(_audit_rows(u1, q.AUDIT_ACTION)) == before_audit
    assert len(_timeline_status_changes(u1, lead_id)) == before_timeline


@pytest.mark.parametrize(
    "current,target",
    [
        ("new", "contacted"),
        ("new", "qualifying"),
        ("contacted", "qualifying"),
        ("qualifying", "qualified"),
        ("qualifying", "unqualified"),
        ("qualifying", "nurture"),
        ("qualified", "qualifying"),
        ("unqualified", "qualifying"),
        ("nurture", "qualifying"),
    ],
)
def test_legal_transitions_write_audit_and_timeline(two_users, current, target):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    _set_status_direct(u1, lead_id, current)
    updated, error = q.request_status_change(
        u1, lead_id, target, actor="user", trigger="agent_action",
    )
    assert error is None, error
    assert updated["status"] == target
    audits = _audit_rows(u1, q.AUDIT_ACTION)
    assert len(audits) == 1
    assert audits[0]["gate_decision"] == "ALLOW"
    assert current in (audits[0]["gate_reason"] or "")
    assert target in (audits[0]["gate_reason"] or "")
    assert audits[0]["target_type"] == "lead"
    assert audits[0]["target_id"] == lead_id
    changes = _timeline_status_changes(u1, lead_id)
    assert len(changes) == 1
    assert changes[0]["summary"].startswith("Lead status changed from")


def test_set_lead_status_guard_refuses_machine_states(two_users):
    """Direct writes to the four machine states are refused outside
    the qualification module (section 2.5 enforcement)."""
    u1, _ = two_users
    lead_id = _make_lead(u1)
    for target in ("qualified", "unqualified", "qualifying", "nurture"):
        updated, error = crm_db.set_lead_status(u1, lead_id, target)
        assert updated is None
        assert error == q.QUALIFICATION_GUARD_MESSAGE
    assert _lead(u1, lead_id)["status"] == "new"
    # Out-of-lane statuses are untouched by the guard.
    updated, error = crm_db.set_lead_status(u1, lead_id, "appointment_scheduled")
    assert error is None and updated["status"] == "appointment_scheduled"
    updated, error = crm_db.set_lead_status(u1, lead_id, "contacted")
    assert error is None and updated["status"] == "contacted"


def test_request_status_change_refuses_out_of_lane_targets(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    updated, error = q.request_status_change(u1, lead_id, "closed_won")
    assert updated is None
    assert error is not None and "outside the qualification lane" in error
    assert _lead(u1, lead_id)["status"] == "new"


# ---------------------------------------------------------------------------
# Field writes drive the state
# ---------------------------------------------------------------------------

def test_hot_fields_land_qualified_via_qualifying(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, error = q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert error is None, error
    assert result["score"] == 75
    assert result["band"] == "Hot"
    assert result["qualified"] is True
    assert result["transition"] == "qualified"
    assert result["current_state"] == "qualified"
    lead = _lead(u1, lead_id)
    assert lead["status"] == "qualified"
    assert lead["qualification_score"] == 75
    assert json.loads(lead["qualification_score_json"])["budget"]["points"] == 25
    # Stepped through qualifying: two audit rows + two timeline rows.
    assert len(_audit_rows(u1, q.AUDIT_ACTION)) == 2
    assert len(_timeline_status_changes(u1, lead_id)) == 2


def test_partial_fields_land_qualifying(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, error = q.set_qualification_fields(
        u1, lead_id, {"budget": "300-500k"}, actor="user",
    )
    assert error is None, error
    assert result["score"] == 15
    assert result["band"] == "Cold"
    assert result["transition"] == "qualifying"
    assert _lead(u1, lead_id)["status"] == "qualifying"


def test_threshold_counterexample_preapproval_no_lands_nurture(two_users):
    """Score >= 60 alone is NOT enough: pre-approval no blocks
    qualified and the lead lands in nurture with the failed
    condition named."""
    u1, _ = two_users
    lead_id = _make_lead(u1)
    fields = {
        "budget": "over-1m",  # 30
        "timeline": "immediate",  # 25
        "preapproval": "no",  # 0
        "motivation": "relocating",  # 10
    }  # 65 >= 60, but pre-approval no
    result, error = q.set_qualification_fields(u1, lead_id, fields, actor="user")
    assert error is None, error
    assert result["score"] == 65
    assert result["band"] == "Hot"
    assert result["qualified"] is False
    assert any("pre-approval no" in r and "not met" in r
               for r in result["qualified_reasons"])
    assert result["current_state"] == "nurture"
    assert _lead(u1, lead_id)["status"] == "nurture"


def test_threshold_counterexample_timeline_later_lands_nurture(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    fields = {
        "budget": "over-1m",  # 30
        "timeline": "later",  # 5
        "preapproval": "yes",  # 25
        "motivation": "relocating",  # 10
    }  # 70 >= 60, but timeline later
    result, error = q.set_qualification_fields(u1, lead_id, fields, actor="user")
    assert error is None, error
    assert result["score"] == 70
    assert result["qualified"] is False
    assert any("timeline later" in r and "not met" in r
               for r in result["qualified_reasons"])
    assert _lead(u1, lead_id)["status"] == "nurture"


# ---------------------------------------------------------------------------
# Unqualified rules + re-open
# ---------------------------------------------------------------------------

def test_unqualified_budget_under_300k_and_browsing(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, error = q.set_qualification_fields(
        u1, lead_id,
        {"budget": "under-300k", "timeline": "browsing"},
        actor="user",
    )
    assert error is None, error
    assert result["score"] == 5
    assert result["current_state"] == "unqualified"
    assert _lead(u1, lead_id)["status"] == "unqualified"


def test_unqualified_motivation_other_two_contacts(two_users):
    """Motivation other + no budget/timeline + >= 2 contacts."""
    u1, _ = two_users
    lead_id = _make_lead(u1)
    _insert_sms(u1, lead_id, "outbound", "sent", _iso(minutes_ago=30))
    _insert_sms(u1, lead_id, "inbound", "received", _iso(minutes_ago=10))
    result, error = q.set_qualification_fields(
        u1, lead_id, {"motivation": "other"}, actor="user",
    )
    assert error is None, error
    assert result["current_state"] == "unqualified"
    assert _lead(u1, lead_id)["status"] == "unqualified"


def test_unqualified_motivation_other_needs_two_contacts(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    _insert_sms(u1, lead_id, "inbound", "received", _iso(minutes_ago=10))
    result, error = q.set_qualification_fields(
        u1, lead_id, {"motivation": "other"}, actor="user",
    )
    assert error is None, error
    # Only 1 contact: the rule does not fire; still gathering.
    assert result["current_state"] == "qualifying"


def test_reopen_on_field_change_then_resettles(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, error = q.set_qualification_fields(
        u1, lead_id,
        {"budget": "under-300k", "timeline": "browsing"},
        actor="user",
    )
    assert _lead(u1, lead_id)["status"] == "unqualified"
    # New evidence re-opens to qualifying only.
    result, error = q.set_qualification_fields(
        u1, lead_id, {"budget": "500k-1m"}, actor="user",
    )
    assert error is None, error
    assert result["transition"] == "qualifying"
    assert _lead(u1, lead_id)["status"] == "qualifying"
    # Next field change re-settles by rule: 25 + 15 + 25 + 10 = 75.
    result, error = q.set_qualification_fields(
        u1, lead_id,
        {"timeline": "soon", "preapproval": "yes", "motivation": "relocating"},
        actor="user",
    )
    assert error is None, error
    assert result["transition"] == "qualified"
    assert _lead(u1, lead_id)["status"] == "qualified"
    assert _lead(u1, lead_id)["qualification_score"] == 75


def test_new_exchange_alone_does_not_reopen(two_users):
    """A new exchange never re-opens a terminal state — only a
    qualification-field change does."""
    import autonomy

    u1, _ = two_users
    lead_id = _make_lead(u1)
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert _lead(u1, lead_id)["status"] == "qualified"
    _insert_sms(u1, lead_id, "outbound", "sent", _iso(minutes_ago=30))
    _insert_sms(u1, lead_id, "inbound", "received", _iso(minutes_ago=5))
    autonomy.apply_inbound_side_effects(
        u1, lead_id,
        {"intent": "question", "suggested_lead_status": "qualified"},
        source="ai_sms",
    )
    assert _lead(u1, lead_id)["status"] == "qualified"


# ---------------------------------------------------------------------------
# Recompute: field changes + signals, on-read == stored
# ---------------------------------------------------------------------------

def test_recompute_field_change_reply_and_booking(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    result, _ = q.set_qualification_fields(
        u1, lead_id, {"budget": "300-500k"}, actor="user",
    )
    assert _lead(u1, lead_id)["qualification_score"] == 15
    # Field change -> stored score changes.
    result, _ = q.set_qualification_fields(
        u1, lead_id, {"budget": "500k-1m"}, actor="user",
    )
    assert _lead(u1, lead_id)["qualification_score"] == 25
    # Reply event -> +10 (inbound after the first outbound).
    _insert_sms(u1, lead_id, "outbound", "sent", _iso(minutes_ago=30))
    _insert_sms(u1, lead_id, "inbound", "received", _iso(minutes_ago=5))
    new_score = q.refresh_qualification_score(u1, lead_id)
    assert new_score == 35
    assert _lead(u1, lead_id)["qualification_score"] == 35
    assert q.evaluate_lead(u1, lead_id)["score"] == 35
    # Booking event -> +10 (appointment creation refreshes the cache).
    appt_id, error = crm_db.create_appointment(
        u1,
        {"lead_id": lead_id, "appointment_type": "phone_call",
         "start_at": _iso(minutes_ago=-60)},
    )
    assert error is None, error
    assert _lead(u1, lead_id)["qualification_score"] == 45
    evaluation = q.evaluate_lead(u1, lead_id)
    assert evaluation["score"] == 45
    assert evaluation["stored_score"] == 45
    assert evaluation["breakdown"]["replied"] == {"value": True, "points": 10}
    assert evaluation["breakdown"]["booked"] == {"value": True, "points": 10}


def test_inbound_before_first_outbound_scores_zero(two_users):
    """An inbound that arrives before the tenant ever reached out is
    a new inbound, not a reply to an outreach: 0 on the signal."""
    u1, _ = two_users
    lead_id = _make_lead(u1)
    _insert_sms(u1, lead_id, "inbound", "received", _iso(minutes_ago=10))
    new_score = q.refresh_qualification_score(u1, lead_id)
    assert new_score == 0
    assert q.evaluate_lead(u1, lead_id)["signals"]["replied"] is False


def test_booked_via_voice_call_appointment_requested(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    _insert_voice_call(u1, lead_id, "outbound", _iso(minutes_ago=30),
                       appointment_requested=1)
    assert q.evaluate_lead(u1, lead_id)["signals"]["booked"] is True
    assert q.evaluate_lead(u1, lead_id)["score"] == 10


def test_cancelled_appointment_does_not_count_as_booked(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    appt_id, error = crm_db.create_appointment(
        u1,
        {"lead_id": lead_id, "appointment_type": "phone_call",
         "start_at": _iso(minutes_ago=-60), "status": "cancelled"},
    )
    assert error is None, error
    assert q.evaluate_lead(u1, lead_id)["signals"]["booked"] is False


# ---------------------------------------------------------------------------
# Section 2.5 regression: the model can no longer silently write qualified
# ---------------------------------------------------------------------------

def test_model_suggested_qualified_no_longer_writes(two_users):
    """Pre-III-2, ROUTINE_LEAD_STATUSES contained 'qualified' and the
    inbound path applied a model-suggested status directly. Now the
    suggestion is checked against the evaluator: a lead with no
    qualifying fields stays put."""
    import autonomy

    u1, _ = two_users
    assert "qualified" not in autonomy.ROUTINE_LEAD_STATUSES
    lead_id = _make_lead(u1, status="contacted")
    result = autonomy.apply_inbound_side_effects(
        u1, lead_id,
        {"intent": "buying", "suggested_lead_status": "qualified"},
        source="ai_sms",
    )
    assert _lead(u1, lead_id)["status"] == "contacted"
    assert "status" not in result["applied"]
    assert _audit_rows(u1, q.AUDIT_ACTION) == []


def test_model_suggested_status_applies_only_when_evaluator_agrees(two_users):
    """A suggested machine state applies when — and only when — the
    evaluator yields it."""
    import autonomy

    u1, _ = two_users
    lead_id = _make_lead(u1, status="qualifying")
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    # Lead is qualified now; re-open to qualifying with a field change
    # that keeps the evaluator yielding qualified on the next check.
    _set_status_direct(u1, lead_id, "qualifying")
    result = autonomy.apply_inbound_side_effects(
        u1, lead_id,
        {"intent": "buying", "suggested_lead_status": "qualified"},
        source="ai_sms",
    )
    assert _lead(u1, lead_id)["status"] == "qualified"
    assert "status" in result["applied"]
    assert len(_audit_rows(u1, q.AUDIT_ACTION)) >= 1


def test_model_suggested_nurture_that_evaluator_rejects_stays_suggestion(two_users):
    import autonomy

    u1, _ = two_users
    lead_id = _make_lead(u1, status="qualifying")
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert _lead(u1, lead_id)["status"] == "qualified"
    _set_status_direct(u1, lead_id, "qualifying")
    # Evaluator yields qualified (score 75, conditions met), not nurture.
    result = autonomy.apply_inbound_side_effects(
        u1, lead_id,
        {"intent": "buying", "suggested_lead_status": "nurture"},
        source="ai_sms",
    )
    assert _lead(u1, lead_id)["status"] == "qualifying"
    assert "status" not in result["applied"]


def test_not_interested_routes_through_evaluator(two_users):
    """'Not interested' is evidence, not a status write: a qualifying
    lead whose evaluator yields nurture moves with an audit row; a
    lead the evaluator still yields qualified for does not move."""
    import autonomy

    u1, _ = two_users
    # Case 1: qualifying, all fields known, score below threshold
    # (re-opened from qualified by a field change that dropped the
    # score) -> evaluator yields nurture -> transition fires.
    lead_id = _make_lead(u1)
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert _lead(u1, lead_id)["status"] == "qualified"
    q.set_qualification_fields(u1, lead_id, {"budget": "under-300k"}, actor="user")
    assert _lead(u1, lead_id)["status"] == "qualifying"
    assert q.evaluate_lead(u1, lead_id)["derived_state"] == "nurture"
    result = autonomy.apply_inbound_side_effects(
        u1, lead_id, {"intent": "not interested"}, source="ai_sms",
    )
    assert _lead(u1, lead_id)["status"] == "nurture"
    assert "status" in result["applied"]
    assert len(_audit_rows(u1, q.AUDIT_ACTION)) >= 1

    # Case 2: qualified lead says not interested -> evaluator still
    # yields qualified -> no move, no new audit row.
    lead2 = _make_lead(u1)
    q.set_qualification_fields(u1, lead2, HOT_FIELDS, actor="user")
    before = len(_audit_rows(u1, q.AUDIT_ACTION))
    autonomy.apply_inbound_side_effects(
        u1, lead2, {"intent": "not interested"}, source="ai_sms",
    )
    assert _lead(u1, lead2)["status"] == "qualified"
    assert len(_audit_rows(u1, q.AUDIT_ACTION)) == before


def test_contacted_on_exchange_behaviour_kept(two_users):
    """The exchange rule still moves new/attempting_contact to
    contacted on an inbound analysis with no status suggestion."""
    import autonomy

    u1, _ = two_users
    lead_id = _make_lead(u1, status="new")
    result = autonomy.apply_inbound_side_effects(
        u1, lead_id, {"intent": "question"}, source="ai_sms",
    )
    assert _lead(u1, lead_id)["status"] == "contacted"
    assert "status" in result["applied"]


# ---------------------------------------------------------------------------
# Suggestion path (model suggestion-only)
# ---------------------------------------------------------------------------

def test_suggestion_renders_without_changing_anything(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    insight_id, error = q.store_qualification_suggestion(
        u1, lead_id, None, dict(HOT_FIELDS), confidence=0.8,
    )
    assert error is None, error
    assert insight_id is not None
    lead = _lead(u1, lead_id)
    assert lead["status"] == "new"
    assert lead["qualification_score"] == 0
    assert lead["qualification_budget"] == "unknown"
    found = q.find_pending_qualification_suggestion(u1, lead_id)
    assert found is not None
    assert found["id"] == insight_id
    assert found["qualification_suggestion"]["fields"] == HOT_FIELDS
    assert found["qualification_suggestion"]["confidence"] == 0.8


def test_suggestion_confirm_changes_fields_score_state_together(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    insight_id, _ = q.store_qualification_suggestion(
        u1, lead_id, None, dict(HOT_FIELDS), confidence=0.9,
    )
    result, error = q.confirm_qualification_suggestion(u1, insight_id, actor="user")
    assert error is None, error
    assert result["score"] == 75
    assert result["transition"] == "qualified"
    lead = _lead(u1, lead_id)
    assert lead["status"] == "qualified"
    assert lead["qualification_budget"] == "500k-1m"
    assert lead["qualification_score"] == 75
    assert len(_audit_rows(u1, q.AUDIT_ACTION)) == 2  # via qualifying
    assert len(_timeline_status_changes(u1, lead_id)) == 2
    with db.get_db() as conn:
        row = dict(conn.execute(
            "SELECT status FROM lead_insights WHERE id = ?", (insight_id,),
        ).fetchone())
    assert row["status"] == "applied"
    assert q.find_pending_qualification_suggestion(u1, lead_id) is None


def test_suggestion_dismiss_stops_rendering_row_kept(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    insight_id, _ = q.store_qualification_suggestion(
        u1, lead_id, None, dict(HOT_FIELDS),
    )
    result, error = q.dismiss_qualification_suggestion(u1, insight_id)
    assert error is None, error
    assert result["status"] == "dismissed"
    assert q.find_pending_qualification_suggestion(u1, lead_id) is None
    with db.get_db() as conn:
        row = dict(conn.execute(
            "SELECT * FROM lead_insights WHERE id = ?", (insight_id,),
        ).fetchone())
    assert row["status"] == "dismissed"
    assert "qualification_suggestion" in (row["raw_json"] or "")
    assert _lead(u1, lead_id)["status"] == "new"


def test_coach_response_carries_qualification_suggestion():
    """The inbound coach normalizes a qualification_suggestion from
    model JSON into the analysis dict and raw_json (stored on the
    lead_insights row by the inbound path). Invalid values drop."""
    import sms_coach

    payload = json.dumps({
        "summary": "Wants a $600k home soon, pre-approved.",
        "intent": "buying",
        "confidence": 0.8,
        "suggested_lead_status": "contacted",
        "qualification_suggestion": {
            "budget": "500k-1m",
            "timeline": "soon",
            "preapproval": "yes",
            "motivation": "relocating",
            "confidence": 0.8,
        },
    })
    analysis = sms_coach.validate_coach_response(payload)
    assert analysis["qualification_suggestion"] == {
        "confidence": 0.8,
        "budget": "500k-1m",
        "timeline": "soon",
        "preapproval": "yes",
        "motivation": "relocating",
    }
    raw = json.loads(analysis["raw_json"])
    assert raw["qualification_suggestion"]["budget"] == "500k-1m"

    bad = json.dumps({
        "summary": "x", "intent": "buying",
        "qualification_suggestion": {"budget": "millions", "timeline": "soon"},
    })
    analysis = sms_coach.validate_coach_response(bad)
    assert analysis["qualification_suggestion"] == {
        "confidence": None, "timeline": "soon",
    }


def test_stored_coach_suggestion_parseable_from_insight_row(two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    insight_id = db.create_lead_insight(
        lead_id, u1, None,
        {
            "summary": "Lead replied.",
            "intent": "buying",
            "next_best_step": "Review suggestion.",
            "recommended_action": "Review suggestion.",
            "suggested_reply": "",
            "home_value_pitch": None,
            "confidence_score": 0.8,
            "requires_manual_review": False,
            "escalation_topics": [],
            "raw_json": json.dumps({
                "qualification_suggestion": dict(HOT_FIELDS),
            }),
        },
        model="claude",
        status="pending",
    )
    found = q.find_pending_qualification_suggestion(u1, lead_id)
    assert found is not None and found["id"] == insight_id
    assert found["qualification_suggestion"]["fields"] == HOT_FIELDS


# ---------------------------------------------------------------------------
# No-SMS guarantee
# ---------------------------------------------------------------------------

def test_qualification_ops_create_no_sms_rows(two_users):
    u1, _ = two_users
    before = {t: _table_count(t) for t in
              ("harness_jobs", "sms_messages", "harness_approvals")}
    lead_id = _make_lead(u1)
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    q.refresh_qualification_score(u1, lead_id)
    q.evaluate_lead(u1, lead_id)
    insight_id, _ = q.store_qualification_suggestion(
        u1, lead_id, None, dict(WARM_FIELDS),
    )
    q.confirm_qualification_suggestion(u1, insight_id, actor="user")
    q.request_status_change(u1, lead_id, "qualifying", actor="user")
    after = {t: _table_count(t) for t in
             ("harness_jobs", "sms_messages", "harness_approvals")}
    assert after == before


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------

def test_evaluate_lead_is_tenant_scoped(two_users):
    u1, u2 = two_users
    lead_id = _make_lead(u1)
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert q.evaluate_lead(u2, lead_id) is None
    updated, error = q.request_status_change(u2, lead_id, "qualifying")
    assert updated is None and error == "Lead not found."
    result, error = q.set_qualification_fields(u2, lead_id, WARM_FIELDS, actor="user")
    assert result is None and error == "Lead not found."


# ---------------------------------------------------------------------------
# API routes + lead detail page
# ---------------------------------------------------------------------------

def test_status_api_refuses_machine_state_targets(app_client, two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    for target in ("qualified", "unqualified", "qualifying", "nurture"):
        res = app_client.post(
            f"/api/crm/leads/{lead_id}/status", json={"status": target},
        )
        assert res.status_code == 400
        assert "Set qualification fields instead" in res.get_json()["error"]
    assert _lead(u1, lead_id)["status"] == "new"
    # Out-of-lane targets still work through the API.
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/status", json={"status": "contacted"},
    )
    assert res.status_code == 200
    assert _lead(u1, lead_id)["status"] == "contacted"


def test_qualification_api_round_trip(app_client, two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/qualification", json=dict(HOT_FIELDS),
    )
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["score"] == 75
    assert body["band"] == "Hot"
    assert body["qualified"] is True
    assert body["state"] == "qualified"
    assert body["breakdown"]["budget"]["points"] == 25
    assert _lead(u1, lead_id)["status"] == "qualified"
    # Invalid value refused with a field-named error.
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/qualification", json={"budget": "millions"},
    )
    assert res.status_code == 400
    assert "Budget" in res.get_json()["error"]


def test_lead_detail_page_renders_qualification(app_client, two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    q.store_qualification_suggestion(u1, lead_id, None, dict(WARM_FIELDS))
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.get(f"/crm/leads/{lead_id}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Qualification" in html
    assert "75 / 110" in html
    assert "Hot" in html
    assert "Suggested qualification" in html
    assert "500k-1m" in html


def test_apply_suggestion_route_refuses_stale_status(app_client, two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    q.set_qualification_fields(u1, lead_id, HOT_FIELDS, actor="user")
    assert _lead(u1, lead_id)["status"] == "qualified"
    # A stale "nurture" suggestion: the evaluator yields qualified.
    insight_id = db.create_lead_insight(
        lead_id, u1, None,
        {
            "summary": "Old suggestion.",
            "intent": "buying",
            "next_best_step": "Follow up.",
            "recommended_action": "Follow up.",
            "suggested_reply": "",
            "home_value_pitch": None,
            "confidence_score": 0.5,
            "requires_manual_review": False,
            "escalation_topics": [],
            "raw_json": json.dumps({"suggested_lead_status": "nurture"}),
        },
        model="claude",
        status="pending",
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(f"/api/crm/insights/{insight_id}/approve-status", json={})
    assert res.status_code == 400
    body = res.get_json()
    assert "stale" in body["error"].lower()
    assert body["current_state"] == "qualified"
    assert _lead(u1, lead_id)["status"] == "qualified"


def test_approve_qualification_suggestion_route(app_client, two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    insight_id, _ = q.store_qualification_suggestion(
        u1, lead_id, None, dict(HOT_FIELDS),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(
        f"/api/crm/insights/{insight_id}/approve-qualification", json={},
    )
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["state"] == "qualified"
    assert _lead(u1, lead_id)["status"] == "qualified"
    # Dismiss route: a second suggestion dismisses cleanly.
    insight2, _ = q.store_qualification_suggestion(
        u1, lead_id, None, dict(WARM_FIELDS),
    )
    res = app_client.post(
        f"/api/crm/insights/{insight2}/dismiss-qualification", json={},
    )
    assert res.status_code == 200
    assert q.find_pending_qualification_suggestion(u1, lead_id) is None


# ---------------------------------------------------------------------------
# Ask TopAI status action routes machine targets through the evaluator
# ---------------------------------------------------------------------------

def test_ask_topai_update_status_machine_target_needs_evaluator(two_users):
    """Ask TopAI's update_lead_status routes qualification-lane
    targets through the evaluator: 'qualified' is not a routine
    status at all (refused by allowed_auto_status), and 'nurture'
    applies only when the evaluator yields it."""
    from ask_topai.actions import execute_update_status

    u1, _ = two_users
    lead_id = _make_lead(u1, status="contacted")
    updated, error, _choices = execute_update_status(
        u1, {"lead_id": lead_id, "status": "qualified"}, None,
    )
    assert updated is None
    assert error is not None and "cannot be applied automatically" in error
    assert _lead(u1, lead_id)["status"] == "contacted"
    # 'nurture' applies when the evaluator yields it: Warm fields on a
    # qualifying lead derive nurture (all fields known, score < 60).
    q.set_qualification_fields(u1, lead_id, WARM_FIELDS, actor="user")
    _set_status_direct(u1, lead_id, "qualifying")
    updated, error, _choices = execute_update_status(
        u1, {"lead_id": lead_id, "status": "nurture"}, None,
    )
    assert error is None, error
    assert updated["status"] == "nurture"
    # ... and is refused when the evaluator yields something else.
    lead2 = _make_lead(u1, status="qualifying")
    q.set_qualification_fields(u1, lead2, HOT_FIELDS, actor="user")
    _set_status_direct(u1, lead2, "qualifying")
    updated, error, _choices = execute_update_status(
        u1, {"lead_id": lead2, "status": "nurture"}, None,
    )
    assert updated is None
    assert error is not None and "evaluator" in error.lower()
    assert _lead(u1, lead2)["status"] == "qualifying"


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def test_pipeline_stages_place_new_statuses():
    from crm_constants import stage_for_status

    assert stage_for_status("qualifying") == "engaged"
    assert stage_for_status("unqualified") == "closed"
    assert stage_for_status("qualified") == "qualified"
    assert stage_for_status("nurture") == "engaged"
