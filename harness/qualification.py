"""Phase III-2 lead qualification state machine + scoring.

CRM-side only. No SMS send, draft, schedule, or suppression; no new
provider or channel; qualification never triggers outreach. This
module imports nothing from ``sms_outbound``,
``harness/sms_handler``, or ``harness/followup_sends``.

Single choke point for the qualification lane
---------------------------------------------
``crm_db.set_lead_status`` stays the low-level writer. Direct writes
to the four machine states (``qualified``, ``unqualified``,
``qualifying``, ``nurture``) from outside this module are refused
by the ``via_qualification`` guard there. The only ways a lead
enters a machine state are:

* ``set_qualification_fields`` — agent entry (UI form) or a
  confirmed suggestion. The fields are written, the score is
  recomputed, the evaluator derives the state, and the transition
  is applied (stepping through ``qualifying`` when the lead enters
  the lane from ``new``/``contacted`` — no transition may skip
  ``qualifying``).
* ``request_status_change`` — validates a transition against the
  table below, then calls ``crm_db.set_lead_status`` with
  ``via_qualification=True``. It validates the *table* only; callers
  that apply a suggested or evaluated state must check the
  evaluator agrees first (see ``evaluate_lead``).

The model stays suggestion-only in every mode: a model-suggested
status or score never writes the lead status by itself.

Re-open
-------
``qualified`` / ``unqualified`` / ``nurture`` -> ``qualifying``
fires only on a qualification-field change (new evidence). A new
exchange alone never re-opens. After re-open the lead sits in
``qualifying`` until the next field change, when the evaluator runs
again and the lead re-settles by rule.

Every legal transition writes two rows: a ``harness_audit`` row
(action ``qualification_transition``, gate decision/reason = the
evaluator verdict) and the ``lead_activities`` ``status_change``
row that ``set_lead_status`` already writes. Refusals write no
audit row.

Scoring
-------
Pure arithmetic — ``score_lead`` takes fields + signals and returns
``(total, breakdown, band)`` with no I/O, no model call, no spend.
``qualified`` needs score >= 60 AND pre-approval in
{yes, in-progress} AND timeline in {immediate, soon}; score alone
is never enough.
"""

from __future__ import annotations

import json

import db

# ---------------------------------------------------------------------------
# Value sets (single source of truth; migration 046 defaults to 'unknown')
# ---------------------------------------------------------------------------

QUALIFICATION_FIELDS = ("budget", "timeline", "preapproval", "motivation")

FIELD_COLUMNS = {
    "budget": "qualification_budget",
    "timeline": "qualification_timeline",
    "preapproval": "qualification_preapproval",
    "motivation": "qualification_motivation",
}

FIELD_VALUE_SETS = {
    "budget": ("unknown", "under-300k", "300-500k", "500k-1m", "over-1m"),
    "timeline": ("unknown", "immediate", "soon", "later", "browsing"),
    "preapproval": ("unknown", "yes", "no", "in-progress"),
    "motivation": (
        "unknown",
        "relocating",
        "first-home",
        "upsizing",
        "downsizing",
        "investing",
        "other",
    ),
}

FIELD_LABELS = {
    "budget": {
        "unknown": "Unknown",
        "under-300k": "Under $300k",
        "300-500k": "$300k–$500k",
        "500k-1m": "$500k–$1m",
        "over-1m": "Over $1m",
    },
    "timeline": {
        "unknown": "Unknown",
        "immediate": "Immediate (0–30 days)",
        "soon": "Soon (1–3 months)",
        "later": "Later (3–6 months)",
        "browsing": "Browsing (6+ months)",
    },
    "preapproval": {
        "unknown": "Unknown",
        "yes": "Yes",
        "no": "No",
        "in-progress": "In progress",
    },
    "motivation": {
        "unknown": "Unknown",
        "relocating": "Relocating",
        "first-home": "First home",
        "upsizing": "Upsizing",
        "downsizing": "Downsizing",
        "investing": "Investing",
        "other": "Other",
    },
}

FIELD_DISPLAY_NAMES = {
    "budget": "Budget",
    "timeline": "Timeline",
    "preapproval": "Pre-approval",
    "motivation": "Motivation",
}

UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Scoring table (verbatim from the PM acceptance criteria, max 110)
# ---------------------------------------------------------------------------

BUDGET_POINTS = {
    "500k-1m": 25,
    "over-1m": 30,
    "300-500k": 15,
    "under-300k": 5,
}

TIMELINE_POINTS = {
    "immediate": 25,
    "soon": 15,
    "later": 5,
    "browsing": 0,
}

PREAPPROVAL_POINTS = {
    "yes": 25,
    "in-progress": 15,
    "no": 0,
}

MOTIVATION_POINTS = {
    "relocating": 10,
    "first-home": 10,
    "upsizing": 10,
    "downsizing": 5,
    "investing": 5,
    "other": 0,
}

REPLIED_POINTS = 10
BOOKED_POINTS = 10
QUALIFIED_THRESHOLD = 60
WARM_MIN = 35

QUALIFIED_PREAPPROVALS = ("yes", "in-progress")
QUALIFIED_TIMELINES = ("immediate", "soon")

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# States governed by the qualification lane. Out-of-lane statuses
# (attempting_contact, appointment_scheduled, appointment_completed,
# under_contract, closed_won, closed_lost, do_not_contact) keep their
# existing paths and are never written by this module.
LANE_STATES = frozenset(
    {"new", "contacted", "qualifying", "qualified", "unqualified", "nurture"}
)

MACHINE_STATES = frozenset({"qualified", "unqualified", "qualifying", "nurture"})

TRANSITIONS = {
    "new": frozenset({"contacted", "qualifying"}),
    "contacted": frozenset({"qualifying"}),
    "qualifying": frozenset({"qualified", "unqualified", "nurture"}),
    "qualified": frozenset({"qualifying"}),
    "unqualified": frozenset({"qualifying"}),
    "nurture": frozenset({"qualifying"}),
}

QUALIFICATION_GUARD_MESSAGE = (
    "Set qualification fields instead — status follows the qualification rules."
)

AUDIT_ACTION = "qualification_transition"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_qualification_fields(fields):
    """Validate a field mapping. Never silently coerces an invalid value.

    Returns ``(cleaned, error)`` — ``cleaned`` maps each supplied field
    to its validated value, ``error`` names the offending field (or is
    None). Unknown field names and values outside the value set are
    both refused.
    """
    if not isinstance(fields, dict):
        return None, "Qualification fields must be an object."
    cleaned = {}
    for name, value in fields.items():
        key = str(name or "").strip().lower()
        if key not in FIELD_VALUE_SETS:
            return None, f"Unknown qualification field: {name}."
        text = str(value or "").strip().lower()
        if text not in FIELD_VALUE_SETS[key]:
            return None, (
                f"Invalid {FIELD_DISPLAY_NAMES[key]} value: {value!r}. "
                f"Expected one of: {', '.join(FIELD_VALUE_SETS[key])}."
            )
        cleaned[key] = text
    if not cleaned:
        return None, "No qualification fields provided."
    return cleaned, None


# ---------------------------------------------------------------------------
# Scoring (pure — no I/O)
# ---------------------------------------------------------------------------

def score_lead(fields, signals=None):
    """Score a lead from its four fields + two signals.

    ``fields`` maps budget/timeline/preapproval/motivation to values
    (missing/unknown = 0 points for that field). ``signals`` maps
    ``replied``/``booked`` to booleans (default False).

    Returns ``(total, breakdown, band)`` where ``breakdown`` is a dict
    of ``field -> {"value": ..., "points": ...}`` for the four fields
    plus ``replied``/``booked``, and ``band`` is Hot (>= 60),
    Warm (35–59) or Cold (< 35). A Hot band is NOT the same as
    qualified — the qualified verdict also needs the pre-approval /
    timeline conditions (see :func:`qualified_verdict`).
    """
    fields = fields or {}
    signals = signals or {}
    budget = str(fields.get("budget") or UNKNOWN).strip().lower()
    timeline = str(fields.get("timeline") or UNKNOWN).strip().lower()
    preapproval = str(fields.get("preapproval") or UNKNOWN).strip().lower()
    motivation = str(fields.get("motivation") or UNKNOWN).strip().lower()
    replied = bool(signals.get("replied"))
    booked = bool(signals.get("booked"))
    breakdown = {
        "budget": {"value": budget, "points": BUDGET_POINTS.get(budget, 0)},
        "timeline": {"value": timeline, "points": TIMELINE_POINTS.get(timeline, 0)},
        "preapproval": {
            "value": preapproval,
            "points": PREAPPROVAL_POINTS.get(preapproval, 0),
        },
        "motivation": {
            "value": motivation,
            "points": MOTIVATION_POINTS.get(motivation, 0),
        },
        "replied": {"value": replied, "points": REPLIED_POINTS if replied else 0},
        "booked": {"value": booked, "points": BOOKED_POINTS if booked else 0},
    }
    total = sum(item["points"] for item in breakdown.values())
    if total >= QUALIFIED_THRESHOLD:
        band = "Hot"
    elif total >= WARM_MIN:
        band = "Warm"
    else:
        band = "Cold"
    return total, breakdown, band


def qualified_verdict(fields, total):
    """The qualified rule: score >= 60 AND pre-approval AND timeline.

    Returns ``(qualified, reasons)`` where ``reasons`` names each
    condition and whether it passed, so the verdict is explainable.
    """
    fields = fields or {}
    preapproval = str(fields.get("preapproval") or UNKNOWN).strip().lower()
    timeline = str(fields.get("timeline") or UNKNOWN).strip().lower()
    score_ok = total >= QUALIFIED_THRESHOLD
    preapproval_ok = preapproval in QUALIFIED_PREAPPROVALS
    timeline_ok = timeline in QUALIFIED_TIMELINES
    reasons = [
        f"score {total} >= {QUALIFIED_THRESHOLD}"
        + (" (met)" if score_ok else " (not met)"),
        f"pre-approval {preapproval}"
        + (" (met)" if preapproval_ok else " (not met — needs yes or in-progress)"),
        f"timeline {timeline}"
        + (" (met)" if timeline_ok else " (not met — needs immediate or soon)"),
    ]
    return score_ok and preapproval_ok and timeline_ok, reasons


# ---------------------------------------------------------------------------
# Loading (tenant-scoped reads only)
# ---------------------------------------------------------------------------

def _lead_fields(lead):
    """The four stored field values for a lead row (defaults unknown)."""
    lead = lead or {}
    return {
        "budget": (lead.get("qualification_budget") or UNKNOWN).strip().lower(),
        "timeline": (lead.get("qualification_timeline") or UNKNOWN).strip().lower(),
        "preapproval": (lead.get("qualification_preapproval") or UNKNOWN).strip().lower(),
        "motivation": (lead.get("qualification_motivation") or UNKNOWN).strip().lower(),
    }


def _load_signals(user_id, lead_id):
    """The two deterministic score signals + contact count for a lead.

    Replied: an inbound SMS (or inbound voice call) recorded AFTER
    the tenant's first outbound contact to the lead. An inbound that
    arrives before the tenant ever reached out scores 0 — it is a
    new inbound, not a reply to an outreach. Boolean, once.
    Booked: a non-cancelled, non-no-show appointments row, or a
    voice call with appointment_requested = 1. Boolean, once.
    Contacts: recorded exchanges — inbound or outbound-sent SMS plus
    voice calls. Used by the motivation-other unqualified rule.
    """
    with db.get_db() as conn:
        first_outbound_sms = conn.execute(
            """
            SELECT MIN(created_at) AS first_at FROM sms_messages
            WHERE user_id = ? AND lead_id = ?
              AND LOWER(COALESCE(direction, '')) = 'outbound'
              AND LOWER(COALESCE(status, '')) IN ('sent', 'delivered')
            """,
            (user_id, lead_id),
        ).fetchone()
        first_outbound_call = conn.execute(
            """
            SELECT MIN(created_at) AS first_at FROM voice_calls
            WHERE user_id = ? AND lead_id = ?
              AND LOWER(COALESCE(direction, '')) = 'outbound'
            """,
            (user_id, lead_id),
        ).fetchone()
        inbound_sms_after = conn.execute(
            """
            SELECT COUNT(*) AS c FROM sms_messages
            WHERE user_id = ? AND lead_id = ?
              AND LOWER(COALESCE(direction, '')) = 'inbound'
              AND created_at > ?
            """,
            (user_id, lead_id, (first_outbound_sms or {}).get("first_at") or "9999"),
        ).fetchone()
        inbound_call_after = conn.execute(
            """
            SELECT COUNT(*) AS c FROM voice_calls
            WHERE user_id = ? AND lead_id = ?
              AND LOWER(COALESCE(direction, '')) = 'inbound'
              AND created_at > ?
            """,
            (user_id, lead_id, (first_outbound_call or {}).get("first_at") or "9999"),
        ).fetchone()
        booked = conn.execute(
            """
            SELECT COUNT(*) AS c FROM appointments
            WHERE user_id = ? AND lead_id = ?
              AND LOWER(COALESCE(status, '')) NOT IN ('cancelled', 'no_show')
            """,
            (user_id, lead_id),
        ).fetchone()
        booked_call = conn.execute(
            """
            SELECT COUNT(*) AS c FROM voice_calls
            WHERE user_id = ? AND lead_id = ? AND COALESCE(appointment_requested, 0) = 1
            """,
            (user_id, lead_id),
        ).fetchone()
        sms_exchanges = conn.execute(
            """
            SELECT COUNT(*) AS c FROM sms_messages
            WHERE user_id = ? AND lead_id = ?
              AND (
                LOWER(COALESCE(direction, '')) = 'inbound'
                OR (
                  LOWER(COALESCE(direction, '')) = 'outbound'
                  AND LOWER(COALESCE(status, '')) IN ('sent', 'delivered')
                )
              )
            """,
            (user_id, lead_id),
        ).fetchone()
        calls = conn.execute(
            "SELECT COUNT(*) AS c FROM voice_calls WHERE user_id = ? AND lead_id = ?",
            (user_id, lead_id),
        ).fetchone()

    def _count(row):
        try:
            return int((row or {}).get("c") or 0)
        except (TypeError, ValueError):
            return int(row[0]) if row else 0

    replied = _count(inbound_sms_after) > 0 or _count(inbound_call_after) > 0
    booked_flag = _count(booked) > 0 or _count(booked_call) > 0
    contacts = _count(sms_exchanges) + _count(calls)
    return {"replied": replied, "booked": booked_flag}, contacts


def _derive_state(fields, signals, contacts, current_status):
    """Derive the lane state from fields + signals (no writes).

    Order matters: qualified first, then unqualified, then
    nurture for a fully assessed lead (all four fields known — the
    assessment is complete and the lead fails the qualified rule,
    whether on score or on the pre-approval/timeline conditions),
    then qualifying while fields are still being gathered, then
    nurture for a contacted lead with no fields yet. A lead that
    fails the qualified rule on its conditions alone (score >= 60
    with pre-approval ``no`` or timeline ``later``/``browsing``)
    therefore lands in nurture, never in qualified.
    """
    total, _breakdown, _band = score_lead(fields, signals)
    qualified, _reasons = qualified_verdict(fields, total)
    if qualified:
        return "qualified"
    budget = fields.get("budget") or UNKNOWN
    timeline = fields.get("timeline") or UNKNOWN
    motivation = fields.get("motivation") or UNKNOWN
    if (budget == "under-300k" and timeline == "browsing") or (
        motivation == "other"
        and budget == UNKNOWN
        and timeline == UNKNOWN
        and contacts >= 2
    ):
        return "unqualified"
    if all((fields.get(f) or UNKNOWN) != UNKNOWN for f in QUALIFICATION_FIELDS):
        return "nurture"
    if any((fields.get(f) or UNKNOWN) != UNKNOWN for f in QUALIFICATION_FIELDS):
        return "qualifying"
    if current_status in {"contacted", "qualifying"}:
        return "nurture"
    return current_status if current_status in LANE_STATES else "new"


def evaluate_lead(user_id, lead_id):
    """Recompute score + derive the machine state for a lead.

    Pure read — writes nothing. Returns the full evaluation the UI
    renders: fields, score, breakdown, band, qualified verdict with
    reasons, current state, derived state, signals, contact count.
    """
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None
    fields = _lead_fields(lead)
    signals, contacts = _load_signals(user_id, lead_id)
    total, breakdown, band = score_lead(fields, signals)
    qualified, reasons = qualified_verdict(fields, total)
    from crm_constants import normalize_lead_status

    current = normalize_lead_status(lead.get("status"))
    derived = _derive_state(fields, signals, contacts, current)
    return {
        "lead_id": lead_id,
        "user_id": user_id,
        "fields": fields,
        "score": total,
        "stored_score": lead.get("qualification_score"),
        "breakdown": breakdown,
        "band": band,
        "qualified": qualified,
        "qualified_reasons": reasons,
        "current_state": current,
        "derived_state": derived,
        "signals": signals,
        "contacts": contacts,
        "status_label": current,
    }


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def request_status_change(user_id, lead_id, target, *, actor="user",
                          trigger="agent_action", gate_reason=None):
    """Validate a lane transition and apply it (table check only).

    The target must be a lane state and the ``current -> target`` jump
    must be in the transition table; anything else is refused with a
    named error and no write and no audit row. A legal transition
    writes the harness_audit row (action
    ``qualification_transition``) plus the lead_activities
    ``status_change`` row that ``set_lead_status`` writes.

    This validates the table, not the evaluator: callers that apply a
    suggested or evaluated state must check ``evaluate_lead`` agrees
    first. Re-open transitions (terminal -> qualifying) are the one
    case where the evaluator deliberately does NOT agree — the fields
    still describe the old state until the next field change.
    """
    import crm_db
    from crm_constants import LEGACY_STATUS_MAP, LEAD_STATUS_SET
    from harness.audit import record_audit

    raw = str(target or "").strip().lower().replace(" ", "_")
    target = LEGACY_STATUS_MAP.get(raw, raw)
    if target not in LEAD_STATUS_SET:
        return None, "Invalid lead status."
    if target not in LANE_STATES:
        return None, (
            f"Status '{target}' is outside the qualification lane — "
            "use the existing status path."
        )
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    from crm_constants import normalize_lead_status

    current = normalize_lead_status(lead.get("status"))
    if current not in LANE_STATES:
        return None, (
            f"Lead is {current}, outside the qualification lane — "
            "the qualification evaluator does not move it."
        )
    if target == current:
        return dict(lead), None
    if target not in TRANSITIONS.get(current, frozenset()):
        return None, (
            f"Illegal qualification transition: {current} -> {target}. "
            "No transition may skip qualifying."
        )
    updated, error = crm_db.set_lead_status(
        user_id, lead_id, target, actor_user_id=user_id,
        from_automation=(actor != "user"), via_qualification=True,
    )
    if error:
        return None, error
    record_audit(
        user_id,
        actor,
        AUDIT_ACTION,
        target_type="lead",
        target_id=lead_id,
        gate_decision="ALLOW",
        gate_reason=gate_reason
        or f"qualification transition {current} -> {target} ({trigger})",
    )
    return updated, None


def apply_derived_state(user_id, lead_id, target, *, actor="user",
                        trigger="agent_action", gate_reason=None):
    """Apply an evaluator-derived state for a lead.

    Same table enforcement as :func:`request_status_change`, but a
    lead entering the lane from ``new``/``contacted`` steps through
    ``qualifying`` instead of attempting a jump that skips it.
    Callers must have checked ``evaluate_lead`` yields ``target``
    first. Returns ``(lead, error)``.
    """
    from crm_constants import normalize_lead_status

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    current = normalize_lead_status(lead.get("status"))
    _updated, _transition, error = _transition_to_derived(
        user_id, lead_id, current, target, actor=actor, trigger=trigger,
        gate_reason=gate_reason or f"evaluator yields {target}",
    )
    if error:
        return None, error
    return db.get_lead(lead_id, user_id), None


def _transition_to_derived(user_id, lead_id, current, derived, *, actor, trigger,
                           gate_reason):
    """Apply a derived state, stepping through ``qualifying`` when the
    lead enters the lane from ``new``/``contacted``.

    Returns ``(lead, transition, error)`` where ``transition`` is the
    final state applied (or None when nothing moved). A direct
    ``new``/``contacted`` -> terminal jump is never written; the lead
    passes through ``qualifying`` (one audit + timeline row per step).
    """
    if derived == current:
        return db.get_lead(lead_id, user_id), None, None
    if derived in TRANSITIONS.get(current, frozenset()):
        updated, error = request_status_change(
            user_id, lead_id, derived, actor=actor, trigger=trigger,
            gate_reason=gate_reason,
        )
        if error:
            return None, None, error
        return updated, derived, None
    if current in {"new", "contacted"} and derived in {
        "qualified", "unqualified", "nurture",
    }:
        stepped, error = request_status_change(
            user_id, lead_id, "qualifying", actor=actor, trigger=trigger,
            gate_reason=f"entered qualifying: {gate_reason}",
        )
        if error:
            return None, None, error
        updated, error = request_status_change(
            user_id, lead_id, derived, actor=actor, trigger=trigger,
            gate_reason=gate_reason,
        )
        if error:
            return stepped, "qualifying", error
        return updated, derived, None
    return db.get_lead(lead_id, user_id), None, None


# ---------------------------------------------------------------------------
# Field writes (the ONLY writer of the four columns + stored score)
# ---------------------------------------------------------------------------

def set_qualification_fields(user_id, lead_id, fields, *, actor="user",
                             trigger="agent_action", source=None):
    """Write qualification fields, recompute the score, apply the state.

    A field write that changes nothing writes nothing and recomputes
    nothing. A write on a lead in a terminal lane state
    (qualified/unqualified/nurture) re-opens it to ``qualifying``
    only — it re-settles by rule on the next field change. A write
    on a lead in ``new``/``contacted``/``qualifying`` applies the
    evaluator's derived state (stepping through ``qualifying`` when
    entering the lane). Out-of-lane leads keep their status; only
    the fields + score are written.

    Returns ``(result, error)``. ``result`` carries the updated lead,
    fields, score, breakdown, band, qualified verdict, state and any
    transition applied.
    """
    cleaned, error = validate_qualification_fields(fields)
    if error:
        return None, error
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    stored = _lead_fields(lead)
    changed = {k: v for k, v in cleaned.items() if stored.get(k) != v}
    if not changed:
        evaluation = evaluate_lead(user_id, lead_id)
        return {
            "lead": dict(lead),
            "fields": stored,
            "score": evaluation["score"],
            "breakdown": evaluation["breakdown"],
            "band": evaluation["band"],
            "qualified": evaluation["qualified"],
            "qualified_reasons": evaluation["qualified_reasons"],
            "current_state": evaluation["current_state"],
            "derived_state": evaluation["derived_state"],
            "changed": {},
            "transition": None,
        }, None

    from crm_constants import normalize_lead_status

    current = normalize_lead_status(lead.get("status"))
    new_fields = dict(stored)
    new_fields.update(changed)
    signals, _contacts = _load_signals(user_id, lead_id)
    total, breakdown, band = score_lead(new_fields, signals)
    qualified, reasons = qualified_verdict(new_fields, total)
    with db.get_db() as conn:
        assignments = ", ".join(f"{FIELD_COLUMNS[k]} = ?" for k in changed)
        params = [changed[k] for k in changed]
        params.extend([total, json.dumps(breakdown), lead_id, user_id])
        conn.execute(
            f"UPDATE leads SET {assignments}, "
            "qualification_score = ?, qualification_score_json = ? "
            "WHERE id = ? AND user_id = ?",
            params,
        )
    gate_reason = (
        f"score {total} >= {QUALIFIED_THRESHOLD};" if qualified else
        f"score {total}; "
    ) + "; ".join(reasons)
    transition = None
    transition_error = None
    if current in {"qualified", "unqualified", "nurture"}:
        # Re-open on new evidence only — re-settles on the next change.
        _updated, transition_error = request_status_change(
            user_id, lead_id, "qualifying", actor=actor, trigger=trigger,
            gate_reason=(
                "re-open on qualification-field change: "
                + ", ".join(f"{k}={v}" for k, v in sorted(changed.items()))
            ),
        )
        transition = "qualifying" if not transition_error else None
    elif current in LANE_STATES:
        derived = _derive_state(new_fields, signals, _contacts, current)
        _updated, transition, transition_error = _transition_to_derived(
            user_id, lead_id, current, derived, actor=actor,
            trigger=trigger, gate_reason=gate_reason,
        )
    # Out-of-lane leads: fields + score written, status untouched.
    updated_lead = db.get_lead(lead_id, user_id)
    return {
        "lead": updated_lead,
        "fields": new_fields,
        "score": total,
        "breakdown": breakdown,
        "band": band,
        "qualified": qualified,
        "qualified_reasons": reasons,
        "current_state": normalize_lead_status((updated_lead or {}).get("status")),
        "derived_state": _derive_state(
            new_fields, signals, _contacts, current
        ) if current in LANE_STATES else current,
        "changed": changed,
        "transition": transition,
        "transition_error": transition_error,
        "source": source,
    }, None


def refresh_qualification_score(user_id, lead_id):
    """Recompute the stored score cache from live fields + signals.

    Best-effort cache refresh for signal events (inbound reply,
    booking): updates the stored score/breakdown only — it never
    changes fields and never moves state. Returns the new score, or
    None when the lead is missing or the refresh cannot run.
    """
    try:
        lead = db.get_lead(lead_id, user_id)
        if not lead:
            return None
        fields = _lead_fields(lead)
        signals, _contacts = _load_signals(user_id, lead_id)
        total, breakdown, _band = score_lead(fields, signals)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE leads SET qualification_score = ?, "
                "qualification_score_json = ? WHERE id = ? AND user_id = ?",
                (total, json.dumps(breakdown), lead_id, user_id),
            )
        return total
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Model suggestion path (suggestion-only; stored in lead_insights)
# ---------------------------------------------------------------------------

def parse_qualification_suggestion(raw_json):
    """Extract a qualification suggestion from an insight's raw_json.

    Returns ``{"fields": {...}, "confidence": ...}`` with only
    valid field values kept, or None when no suggestion is present.
    Invalid suggested values are dropped, never coerced.
    """
    try:
        raw = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    suggestion = raw.get("qualification_suggestion")
    if not isinstance(suggestion, dict):
        return None
    fields = {}
    for key in QUALIFICATION_FIELDS:
        value = str(suggestion.get(key) or "").strip().lower()
        if value and value in FIELD_VALUE_SETS[key]:
            fields[key] = value
    if not fields:
        return None
    confidence = suggestion.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return {"fields": fields, "confidence": confidence}


def find_pending_qualification_suggestion(user_id, lead_id):
    """Newest pending insight carrying a qualification suggestion."""
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM lead_insights
            WHERE user_id = ? AND lead_id = ? AND status = 'pending'
              AND raw_json LIKE '%%qualification_suggestion%%'
            ORDER BY created_at DESC, id DESC
            """,
            (user_id, lead_id),
        ).fetchall()
    for row in rows:
        parsed = parse_qualification_suggestion(dict(row).get("raw_json"))
        if parsed:
            insight = dict(row)
            insight["qualification_suggestion"] = parsed
            return insight
    return None


def store_qualification_suggestion(user_id, lead_id, inbound_message_id,
                                   suggestion, *, summary=None, intent=None,
                                   confidence=None):
    """Store a model qualification suggestion as a pending insight.

    Writes no field, changes no score, moves no state — the
    suggestion renders on the lead record until an agent confirms or
    dismisses it.
    """
    fields, error = validate_qualification_fields(
        {k: v for k, v in (suggestion or {}).items()
         if k in QUALIFICATION_FIELDS}
    )
    if error:
        return None, error
    payload = {
        "summary": summary or "Suggested qualification values from the conversation.",
        "intent": intent or "qualification_suggestion",
        "next_best_step": "Review the suggested qualification values.",
        "recommended_action": "Confirm or dismiss the suggested qualification values.",
        "suggested_reply": "",
        "home_value_pitch": None,
        "confidence_score": confidence,
        "requires_manual_review": False,
        "escalation_topics": [],
        "raw_json": json.dumps({
            "qualification_suggestion": {
                **fields,
                "confidence": confidence,
            }
        }),
    }
    insight_id = db.create_lead_insight(
        lead_id, user_id, inbound_message_id, payload, model="claude",
        status="pending",
    )
    return insight_id, None


def confirm_qualification_suggestion(user_id, insight_id, *, actor="user"):
    """Agent confirms a pending qualification suggestion.

    Applies the suggested values through ``set_qualification_fields``
    (agent action -> evaluator -> possible transition + audit), then
    marks the insight applied. The row is kept for audit.
    """
    insight = db.get_insight(insight_id, user_id)
    if not insight:
        return None, "Suggestion not found."
    if (insight.get("status") or "") != "pending":
        return None, "Suggestion is no longer pending."
    parsed = parse_qualification_suggestion(insight.get("raw_json"))
    if not parsed:
        return None, "Suggestion has no valid qualification values."
    result, error = set_qualification_fields(
        user_id, insight["lead_id"], parsed["fields"], actor=actor,
        trigger="agent_action", source="suggestion_confirmed",
    )
    if error:
        return None, error
    db.update_insight_status(insight_id, user_id, "applied")
    return result, None


def dismiss_qualification_suggestion(user_id, insight_id):
    """Agent dismisses a pending qualification suggestion.

    The row stays for audit; it stops rendering.
    """
    insight = db.get_insight(insight_id, user_id)
    if not insight:
        return None, "Suggestion not found."
    if (insight.get("status") or "") != "pending":
        return None, "Suggestion is no longer pending."
    db.update_insight_status(insight_id, user_id, "dismissed")
    return {"insight_id": insight_id, "status": "dismissed"}, None
