"""Pending appointment requests through the gate (Phase III-4).

Inbound booking requests do NOT auto-execute. A booking request from a
lead creates a pending ``harness_appointment_requests`` row; the
calendar event is created and the lead is told it is booked ONLY after
the agent confirms (one tap in the inbox / approval flow). The AI may
auto-send a confirmation-request text to the lead (a routine send in
Authorized mode), but the calendar write waits for confirmation. This
holds in every mode, including Authorized.

Every request, confirmation, refusal, and booking writes a
``harness_audit`` row and a lead-timeline entry naming the requested
slot, the confirming agent action, and the resulting event (or the
reason it did not happen). The 500-char audit cap is applied at every
write site, as in III-3.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import crm_db
import db
from harness.audit import record_audit

logger = logging.getLogger(__name__)

TOOL = "appointment_request"

REQUEST_STATUSES = frozenset(
    {
        "requested",
        "lead_confirmed",
        "confirming",
        "confirmed",
        "declined",
        "conflicted",
        "expired",
    }
)

# Statuses a confirm attempt may claim (guarded UPDATE).
CLAIMABLE_STATUSES = frozenset({"requested", "lead_confirmed", "conflicted"})

# A pending request expires 7 days after creation (mirrors approval expiry).
EXPIRY_DAYS = 7

# A 'confirming' row older than this is a crashed confirm; the
# re-confirm path may reset it to 'requested' for a fresh attempt.
STALE_CONFIRMING_MINUTES = 60

# Lead replies that confirm the slot (the lead's "yes" marks the request
# lead_confirmed but never creates the event — only the agent tap does).
_LEAD_CONFIRM_PHRASES = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yes please",
        "yes pls",
        "sounds good",
        "that works",
        "works for me",
        "confirmed",
        "confirm",
        "ok",
        "okay",
        "perfect",
        "great",
        "see you then",
    }
)
_LEAD_CONFIRM_RE = re.compile(r"^yes\b", re.I)


class CalendarUnavailableError(RuntimeError):
    """The connected calendar could not be read — a confirm cannot
    verify no-collision, so it refuses (retryable)."""


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _row(row):
    return dict(row) if row is not None else None


def _parse_iso(value):
    """Parse a stored ISO timestamp; None when missing/unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Slot labels + deterministic template texts
# ---------------------------------------------------------------------------


def format_slot_label(start_at_iso, timezone_name):
    """'Tuesday, Nov 24 at 2:00 PM MT' — always names the timezone
    abbreviation so a lead in another timezone is not misled (§8j)."""
    start = _parse_iso(start_at_iso)
    if not start:
        return ""
    try:
        import crm_time

        tz = crm_time.resolve_zone(timezone_name)
        local = start.astimezone(tz)
    except Exception:
        local = start
    label = local.strftime("%A, %b %d at %I:%M %p").replace(" 0", " ")
    abbrev = local.strftime("%Z") or str(timezone_name or "")
    return f"{label} {abbrev}".strip()


def build_confirmation_request_text(slot_start_at, slot_end_at, timezone_name, kind=None):
    """Deterministic template text for the lead BEFORE confirmation.

    Never claims a booking — it holds the slot and says the agent will
    confirm shortly. When no time was parseable it asks for a time.
    """
    if slot_start_at:
        label = format_slot_label(slot_start_at, timezone_name)
        if kind == "window":
            return (
                f"Thanks — I'm holding {label} (or thereabouts) for you. "
                "I'll confirm a time shortly and send you the booking details."
            )
        return (
            f"Thanks — I'm holding {label} for you. "
            "I'll confirm shortly and send you the booking details."
        )
    return (
        "Thanks for reaching out — what time works best for you? "
        "Once we pick a time I'll confirm the booking."
    )


def build_booking_confirmation_text(slot_start_at, timezone_name):
    """Deterministic post-booking confirmation text (sent by the confirm
    action itself; the agent reviews this exact text on the confirm
    screen). Empty when no slot is set — a NULL-slot request has no
    confirmable text, so no approval can be executed for it."""
    label = format_slot_label(slot_start_at, timezone_name)
    if not label:
        return ""
    return (
        f"You're booked for {label}. "
        "Looking forward to speaking with you then!"
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _audit_request(user_id, action, request_id, lead_id, gate_decision,
                   gate_reason, actor="system"):
    """One harness_audit row for an appointment-request event.

    ``request_id`` (the audit column) carries the appointment-request
    id, so the audit row and the timeline entry join (§4).
    """
    try:
        record_audit(
            user_id,
            actor,
            action,
            tool=TOOL,
            target_type="appointment_request",
            target_id=request_id,
            gate_decision=gate_decision,
            gate_reason=(gate_reason or "")[:500],
            request_id=str(request_id),
        )
    except Exception:
        logger.warning(
            "appointment request audit failed tenant=%s request=%s action=%s",
            user_id,
            request_id,
            action,
            exc_info=True,
        )


def _timeline(user_id, lead_id, event_type, summary, payload):
    """One lead-timeline entry for an appointment-request event."""
    try:
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            event_type,
            summary,
            payload,
            actor_user_id=user_id,
        )
    except Exception:
        logger.warning(
            "appointment request timeline failed tenant=%s lead=%s event=%s",
            user_id,
            lead_id,
            event_type,
            exc_info=True,
        )


def _timeline_payload(request, **extra):
    payload = {
        "request_id": request.get("id"),
        "appointment_id": request.get("confirmed_appointment_id"),
        "start_at": request.get("requested_start_at"),
        "end_at": request.get("requested_end_at"),
        "channel": request.get("channel"),
        "calendar_provider": request.get("calendar_provider"),
        "external_event_id": request.get("external_event_id"),
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _slot_text(request, timezone_name):
    label = format_slot_label(request.get("requested_start_at"), timezone_name)
    return label or "no time parsed"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_request(user_id, request_id):
    if not user_id or not request_id:
        return None
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_appointment_requests WHERE id = ? AND user_id = ?",
            (request_id, user_id),
        ).fetchone()
    return _row(row)


def get_request_by_source(user_id, source_ref_type, source_ref_id):
    """Newest request created from one inbound message / voice call
    (the idempotency key for re-runs of the same inbound)."""
    if not user_id or not source_ref_type or source_ref_id is None:
        return None
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM harness_appointment_requests
            WHERE user_id = ? AND source_ref_type = ? AND source_ref_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, source_ref_type, source_ref_id),
        ).fetchone()
    return _row(row)


def _expire_stale(conn, user_id, now_iso):
    """Mark requested/lead_confirmed rows older than 7 days expired,
    with evidence. Runs inside the caller's transaction."""
    cutoff = (_now() - timedelta(days=EXPIRY_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT * FROM harness_appointment_requests
        WHERE user_id = ? AND status IN ('requested', 'lead_confirmed')
          AND created_at < ?
        """,
        (user_id, cutoff),
    ).fetchall()
    expired = []
    for raw in rows:
        request = _row(raw)
        conn.execute(
            "UPDATE harness_appointment_requests SET status = 'expired', updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status IN ('requested', 'lead_confirmed')",
            (now_iso, request["id"], user_id),
        )
        expired.append(request)
    return expired


def expire_stale_requests(user_id):
    """Expire stale pending requests (7 days, derived from created_at).
    Returns the expired rows. Evidence is written for each."""
    expired = []
    with db.get_db() as conn:
        expired = _expire_stale(conn, user_id, _now_iso())
    for request in expired:
        _audit_request(
            user_id,
            "appointment_request_expired",
            request["id"],
            request["lead_id"],
            "ALLOW",
            f"request #{request['id']} expired: "
            f"{_slot_text(request, None)}, 7 days unconfirmed",
        )
        _timeline(
            user_id,
            request["lead_id"],
            "appointment_request_expired",
            f"Appointment request expired: {_slot_text(request, None)}",
            _timeline_payload(request),
        )
        try:
            crm_db.resolve_needs_attention_by_reason(
                user_id,
                request["lead_id"],
                "appointment_requested",
                "Appointment request expired",
            )
        except Exception:
            pass
    return expired


def expire_request(user_id, request_id):
    """Expire one pending request (e.g. its approval expired without a
    decision). Returns the request, or None when not expirable."""
    request = get_request(user_id, request_id)
    if not request or request["status"] not in ("requested", "lead_confirmed", "conflicted"):
        return None
    _set_request_status(user_id, request_id, "expired")
    request = get_request(user_id, request_id)
    _audit_request(
        user_id,
        "appointment_request_expired",
        request_id,
        request["lead_id"],
        "ALLOW",
        f"request #{request_id} expired: {_slot_text(request, None)}, approval expired unconfirmed",
    )
    _timeline(
        user_id,
        request["lead_id"],
        "appointment_request_expired",
        f"Appointment request expired: {_slot_text(request, None)}",
        _timeline_payload(request),
    )
    try:
        crm_db.resolve_needs_attention_by_reason(
            user_id,
            request["lead_id"],
            "appointment_requested",
            "Appointment request expired",
        )
    except Exception:
        pass
    return request


def list_pending_requests(user_id, lead_id=None):
    """Open requests (requested / lead_confirmed / conflicted), newest
    first. Stale rows expire at read time, as approvals do."""
    expire_stale_requests(user_id)
    with db.get_db() as conn:
        if lead_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM harness_appointment_requests
                WHERE user_id = ? AND lead_id = ?
                  AND status IN ('requested', 'lead_confirmed', 'conflicted')
                ORDER BY created_at DESC, id DESC
                """,
                (user_id, lead_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM harness_appointment_requests
                WHERE user_id = ? AND status IN ('requested', 'lead_confirmed', 'conflicted')
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
    return [_row(r) for r in rows]


def list_requests_for_lead(user_id, lead_id):
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM harness_appointment_requests
            WHERE user_id = ? AND lead_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id, lead_id),
        ).fetchall()
    return [_row(r) for r in rows]

# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_request(user_id, lead_id, *, channel, source_ref_type, source_ref_id,
                   raw_request_text, requested_start_at, requested_end_at,
                   provenance, appointment_type="phone_call",
                   reschedules_appointment_id=None, timezone_name=None):
    """Create a pending appointment request (or attach to an existing one).

    Returns ``(request_dict, action)`` where action is "created",
    "existing" (same inbound re-run — idempotent on source_ref), or
    "duplicate" (a new inbound for the same tenant+lead+slot — attached
    to the existing request, no second row). Returns ``(None, error)``
    when the lead does not exist for this tenant.
    """
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "lead_missing"
    existing = get_request_by_source(user_id, source_ref_type, source_ref_id)
    if existing:
        return existing, "existing"
    if requested_start_at:
        with db.get_db() as conn:
            row = conn.execute(
                """
                SELECT * FROM harness_appointment_requests
                WHERE user_id = ? AND lead_id = ? AND requested_start_at = ?
                  AND status IN ('requested', 'lead_confirmed')
                ORDER BY id ASC LIMIT 1
                """,
                (user_id, lead_id, requested_start_at),
            ).fetchone()
        if row:
            request = _row(row)
            _timeline(
                user_id,
                lead_id,
                "appointment_request_duplicate",
                f"Duplicate appointment request attached: {_slot_text(request, timezone_name)}",
                _timeline_payload(request, duplicate_source_ref_id=source_ref_id),
            )
            _audit_request(
                user_id,
                "appointment_request_duplicate",
                request["id"],
                lead_id,
                "ALLOW",
                f"request #{request['id']}: duplicate inbound {source_ref_type} "
                f"#{source_ref_id} attached, same slot {_slot_text(request, timezone_name)}",
            )
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE harness_appointment_requests SET updated_at = ? WHERE id = ?",
                    (_now_iso(), request["id"]),
                )
            return get_request(user_id, request["id"]), "duplicate"

    now_iso = _now_iso()
    no_calendar = 0
    try:
        import calendar_connections_db as cal_db

        if cal_db.get_active_connection(user_id) is None:
            no_calendar = 1
    except Exception:
        no_calendar = 0
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_appointment_requests
                (user_id, lead_id, channel, source_ref_type, source_ref_id,
                 requested_start_at, requested_end_at, requested_window_json,
                 appointment_type, raw_request_text, status,
                 no_calendar_connected, reschedules_appointment_id,
                 sync_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, 'pending', ?, ?)
            """,
            (
                user_id,
                lead_id,
                channel,
                source_ref_type,
                source_ref_id,
                requested_start_at,
                requested_end_at,
                json.dumps(provenance or {})[:4000],
                appointment_type if appointment_type in _appointment_types() else "phone_call",
                (raw_request_text or "")[:2000],
                no_calendar,
                reschedules_appointment_id,
                now_iso,
                now_iso,
            ),
        )
        request_id = cur.lastrowid
    request = get_request(user_id, request_id)
    approval_id, _err = create_confirm_approval(user_id, request_id)
    if approval_id:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE harness_appointment_requests SET approval_id = ? WHERE id = ? AND user_id = ?",
                (approval_id, request_id, user_id),
            )
        request = get_request(user_id, request_id)
    try:
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            "appointment_requested",
            priority="high",
            source_ref_type=source_ref_type,
            source_ref_id=source_ref_id,
            reason_text="Appointment requested — confirm or decline.",
        )
    except Exception:
        logger.warning(
            "appointment request needs-attention failed tenant=%s lead=%s",
            user_id,
            lead_id,
            exc_info=True,
        )
    _audit_request(
        user_id,
        "appointment_request_received",
        request_id,
        lead_id,
        "ALLOW",
        f"request #{request_id}: lead {lead_id}, {_slot_text(request, timezone_name)}, "
        f"source {channel}, parse {(provenance or {}).get('kind') or 'missing'}",
    )
    _timeline(
        user_id,
        lead_id,
        "appointment_requested",
        f"Appointment requested: {_slot_text(request, timezone_name)}",
        _timeline_payload(request),
    )
    return request, "created"


def _appointment_types():
    try:
        from crm_constants import APPOINTMENT_TYPES

        return set(APPOINTMENT_TYPES)
    except Exception:
        return {"phone_call"}


def create_confirm_approval(user_id, request_id, *, slot_start_at=None, slot_end_at=None):
    """Create (or re-create) the ``appointment_request`` approval for a
    request. The draft carries the exact slot and the exact confirmation
    text the agent approves — one tap authorizes the booking and the
    text (§8a). Returns ``(approval_id, error)``.

    A slot override records the new slot on the same request row (a
    re-confirm with a new slot is a new confirm attempt on the same
    request). A stale 'confirming' row (a crashed confirm older than an
    hour) is reset to 'requested' first so the request is confirmable
    again.
    """
    request = get_request(user_id, request_id)
    if not request:
        return None, "not_found"
    now = _now()
    now_iso = now.isoformat()
    if request["status"] == "confirming":
        updated_at = _parse_iso(request.get("updated_at"))
        if updated_at and updated_at <= now - timedelta(minutes=STALE_CONFIRMING_MINUTES):
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE harness_appointment_requests SET status = 'requested', updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND status = 'confirming'",
                    (now_iso, request_id, user_id),
                )
            request = get_request(user_id, request_id)
        else:
            return None, "confirm_in_progress"
    if request["status"] not in ("requested", "lead_confirmed", "conflicted"):
        return None, f"not_confirmable:{request['status']}"
    timezone_name = None
    try:
        timezone_name = db.get_user_timezone(user_id)
    except Exception:
        timezone_name = None
    slot_start = slot_start_at or request.get("requested_start_at")
    slot_end = slot_end_at or request.get("requested_end_at")
    if slot_start_at:
        parsed_start = _parse_iso(slot_start_at)
        if not parsed_start:
            return None, "invalid_slot"
        if parsed_start <= now:
            return None, "slot_in_past"
        if not slot_end:
            try:
                import scheduling

                duration = int(
                    scheduling.get_settings(user_id).get("default_duration_minutes") or 30
                )
            except Exception:
                duration = 30
            slot_end = (parsed_start + timedelta(minutes=duration)).isoformat()
        with db.get_db() as conn:
            conn.execute(
                """
                UPDATE harness_appointment_requests
                SET requested_start_at = ?, requested_end_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (slot_start, slot_end, now_iso, request_id, user_id),
            )
        request = get_request(user_id, request_id)
    text = build_booking_confirmation_text(slot_start, timezone_name)
    draft = {
        "request_id": request_id,
        "lead_id": request["lead_id"],
        "slot_start_at": slot_start,
        "slot_end_at": slot_end,
        "appointment_type": request.get("appointment_type") or "phone_call",
        "confirmation_text": text,
        "reschedules_appointment_id": request.get("reschedules_appointment_id"),
    }
    try:
        from harness.sms_handler import _create_approval

        approval_id = _create_approval(user_id, None, "appointment_request", draft)
    except Exception:
        logger.warning(
            "appointment request approval create failed tenant=%s request=%s",
            user_id,
            request_id,
            exc_info=True,
        )
        return None, "approval_create_failed"
    if not approval_id:
        return None, "approval_create_failed"
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_appointment_requests SET approval_id = ? WHERE id = ? AND user_id = ?",
            (approval_id, request_id, user_id),
        )
    return approval_id, None


# ---------------------------------------------------------------------------
# Claim + collision check
# ---------------------------------------------------------------------------


def claim_request(user_id, request_id):
    """Guarded claim: requested/lead_confirmed/conflicted -> confirming.
    Returns ``(request_dict, prior_status)`` or ``(None, None)`` when the
    claim is lost (a second confirm attempt finds no claimable row)."""
    row = get_request(user_id, request_id)
    if not row or row["status"] not in CLAIMABLE_STATUSES:
        return None, None
    prior = row["status"]
    with db.get_db() as conn:
        cur = conn.execute(
            """
            UPDATE harness_appointment_requests
            SET status = 'confirming', updated_at = ?
            WHERE id = ? AND user_id = ?
              AND status IN ('requested', 'lead_confirmed', 'conflicted')
            """,
            (_now_iso(), request_id, user_id),
        )
        if cur.rowcount != 1:
            return None, None
    return get_request(user_id, request_id), prior


def _revert_request(user_id, request_id, prior_status):
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_appointment_requests SET status = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'confirming'",
            (prior_status, _now_iso(), request_id, user_id),
        )


def check_slot_collisions(user_id, start_at_iso, end_at_iso, *,
                          exclude_request_id=None, exclude_appointment_id=None,
                          include_external=True):
    """Confirm-time collision check, three sources (sketch §2.3).

    (a) internal appointments (with the account buffer), (b) other
    pending/confirmed requests for the same slot, (c) the connected
    calendar's events. Returns a list of collision dicts
    ``{source, id, summary, start_at, end_at}`` (empty when clear).
    Raises :class:`CalendarUnavailableError` when the external calendar
    cannot be read — a confirm cannot verify no-collision then.
    """
    import scheduling

    start = _parse_iso(start_at_iso)
    end = _parse_iso(end_at_iso)
    if not start or not end:
        raise ValueError("invalid slot")
    settings = scheduling.get_settings(user_id)
    buffer_minutes = int(settings.get("buffer_minutes") or 0)
    pad = timedelta(minutes=buffer_minutes)
    collisions = []

    def _overlaps(other_start, other_end):
        return start < (other_end + pad) and end > (other_start - pad)

    for busy_start, busy_end, appt in scheduling.busy_intervals(
        user_id, exclude_appointment_id=exclude_appointment_id
    ):
        if _overlaps(busy_start, busy_end):
            collisions.append(
                {
                    "source": "internal",
                    "id": appt.get("id"),
                    "summary": f"appointment #{appt.get('id')}",
                    "start_at": busy_start.isoformat(),
                    "end_at": busy_end.isoformat(),
                }
            )
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM harness_appointment_requests
            WHERE user_id = ? AND status IN ('requested', 'lead_confirmed', 'confirmed')
              AND requested_start_at IS NOT NULL AND requested_end_at IS NOT NULL
            """,
            (user_id,),
        ).fetchall()
    for raw in rows:
        other = _row(raw)
        if exclude_request_id is not None and other["id"] == exclude_request_id:
            continue
        other_start = _parse_iso(other.get("requested_start_at"))
        other_end = _parse_iso(other.get("requested_end_at"))
        if not other_start or not other_end:
            continue
        if _overlaps(other_start, other_end):
            collisions.append(
                {
                    "source": "request",
                    "id": other["id"],
                    "summary": f"appointment request #{other['id']}",
                    "start_at": other_start.isoformat(),
                    "end_at": other_end.isoformat(),
                }
            )
    if include_external:
        try:
            import calendar_connections_db as cal_db

            provider, credentials = cal_db.resolve_calendar_provider(user_id)
        except Exception:
            raise CalendarUnavailableError(
                "calendar unavailable — cannot verify no-collision"
            )
        if provider is not None:
            window_start = (start - pad).isoformat()
            window_end = (end + pad).isoformat()
            try:
                events = provider.list_events(
                    credentials=credentials, start_utc=window_start, end_utc=window_end
                )
            except Exception as exc:
                raise CalendarUnavailableError(
                    "calendar unavailable — cannot verify no-collision"
                ) from exc
            for event in events or []:
                event_start = _parse_iso(event.get("start_at"))
                event_end = _parse_iso(event.get("end_at"))
                if not event_start or not event_end:
                    continue
                if _overlaps(event_start, event_end):
                    collisions.append(
                        {
                            "source": "external",
                            "id": event.get("id"),
                            "summary": f"calendar event {event.get('id')}",
                            "start_at": event_start.isoformat(),
                            "end_at": event_end.isoformat(),
                        }
                    )
    return collisions

# ---------------------------------------------------------------------------
# Confirm / decline
# ---------------------------------------------------------------------------


def _set_request_status(user_id, request_id, status):
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_appointment_requests SET status = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (status, _now_iso(), request_id, user_id),
        )


def _refuse(user_id, request, reason):
    """Audit + timeline evidence for a refused confirm (no booking)."""
    _audit_request(
        user_id,
        "appointment_booking_refused",
        request["id"],
        request["lead_id"],
        "BLOCK",
        f"request #{request['id']} refused: {reason}",
    )
    _timeline(
        user_id,
        request["lead_id"],
        "appointment_booking_refused",
        f"Appointment booking refused: {reason}",
        _timeline_payload(request),
    )


def _confirm_policy_check(user_id, lead):
    """Fresh policy-gate re-check for the confirmation text, mirroring
    the approved follow-up send path: consent, opt-out, suppression,
    quiet hours, rate limit, pause, and the spend cap, all read fresh.
    The agent's confirm tap is the human approval (mode authorized).
    Returns ``(decision, gate_reason)``."""
    try:
        from harness.approvals import _follow_up_execution_gate_context
        from harness.policy import decide

        context = _follow_up_execution_gate_context(user_id, lead)
        decision, gate_reason = decide(user_id, "send_sms", "send_sms", context)
        return str(decision or "BLOCK").upper(), gate_reason or ""
    except Exception:
        logger.exception(
            "appointment confirm policy check failed tenant=%s", user_id
        )
        return "BLOCK", "policy_error"


def _prior_external_event_id(user_id, appointment_id):
    """The external event id recorded when this appointment was booked
    (so a reschedule updates the event instead of duplicating it)."""
    if not appointment_id:
        return None
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT external_event_id FROM harness_appointment_requests
            WHERE user_id = ? AND confirmed_appointment_id = ?
              AND external_event_id IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, appointment_id),
        ).fetchone()
    if not row:
        return None
    try:
        return row["external_event_id"]
    except (KeyError, TypeError, IndexError):
        return row[0]


def confirm_request(user_id, request_id, *, correlation_id=None):
    """Run one confirm attempt for a pending request.

    Claim -> no-calendar check -> policy re-check for the confirmation
    text -> collision re-check (three sources) -> book (internal
    appointment + lead status via the existing path) -> external write
    -> confirmation text. Returns a result dict; the caller (the
    approval execution wrapper) maps it onto the approval status.
    A collision refuses with evidence and marks the request
    ``conflicted``; a transient failure (calendar unreadable) reverts
    the claim and is retryable; a hard policy block refuses the
    confirm without booking.
    """
    request = get_request(user_id, request_id)
    if not request:
        return {"ok": False, "reason": "not_found", "retryable": False}
    claimed, prior_status = claim_request(user_id, request_id)
    if not claimed:
        return {"ok": False, "reason": "already_claimed", "retryable": False}
    request = claimed
    lead_id = request["lead_id"]
    slot_start = request.get("requested_start_at")
    slot_end = request.get("requested_end_at")
    if not slot_start:
        _revert_request(user_id, request_id, prior_status)
        return {"ok": False, "reason": "no_slot", "retryable": False}
    try:
        timezone_name = db.get_user_timezone(user_id)
    except Exception:
        timezone_name = None

    # No-calendar check: the confirm action is not offered while no
    # calendar is connected (criterion 4) — the request stays pending.
    try:
        import calendar_connections_db as cal_db

        provider, credentials = cal_db.resolve_calendar_provider(user_id)
    except Exception:
        provider, credentials = None, None
    if provider is None:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE harness_appointment_requests SET no_calendar_connected = 1, "
                "updated_at = ? WHERE id = ? AND user_id = ?",
                (_now_iso(), request_id, user_id),
            )
        _audit_request(
            user_id,
            "appointment_request_pending_no_calendar",
            request_id,
            lead_id,
            "BLOCK",
            f"request #{request_id} pending: no calendar connected",
        )
        _revert_request(user_id, request_id, prior_status)
        return {"ok": False, "reason": "no_calendar_connected", "retryable": False}

    # Policy re-check for the confirmation text, with a fresh lead read.
    # A hard block refuses the confirm without booking: a confirmed
    # booking the lead is never told about is a broken commitment.
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        _revert_request(user_id, request_id, prior_status)
        return {"ok": False, "reason": "lead_missing", "retryable": False}
    decision, gate_reason = _confirm_policy_check(user_id, lead)
    if decision in ("BLOCK", "DRAFT"):
        _revert_request(user_id, request_id, prior_status)
        _refuse(user_id, request, f"policy_{decision.lower()}: {gate_reason}")
        return {"ok": False, "reason": gate_reason, "retryable": False}
    if decision == "DEFER" and "quiet hours" not in (gate_reason or "").lower():
        # Rate limit: retryable — the worker retries with backoff.
        _revert_request(user_id, request_id, prior_status)
        return {"ok": False, "reason": gate_reason, "retryable": True}
    # DEFER quiet-hours: proceed; the text is scheduled, not sent now.

    # Collision re-check, fresh (three sources).
    try:
        collisions = check_slot_collisions(
            user_id,
            slot_start,
            slot_end,
            exclude_request_id=request_id,
            exclude_appointment_id=request.get("reschedules_appointment_id"),
        )
    except CalendarUnavailableError as exc:
        _revert_request(user_id, request_id, prior_status)
        _refuse(user_id, request, str(exc))
        return {"ok": False, "reason": str(exc), "retryable": True}
    if collisions:
        first = collisions[0]
        _set_request_status(user_id, request_id, "conflicted")
        reason = (
            f"collision with {first['source']} {first['summary']} "
            f"({first['start_at']})"
        )
        _refuse(user_id, get_request(user_id, request_id), reason)
        return {
            "ok": False,
            "reason": reason,
            "retryable": False,
            "conflicted": True,
        }

    # Book: internal appointment first (the booking is the commitment),
    # then the request row, then the lead status, then the external
    # write and the confirmation text. A crash between the steps leaves
    # the appointment standing with the request still 'confirming';
    # the re-confirm path recovers a stale 'confirming' row (§8).
    import scheduling

    settings = scheduling.get_settings(user_id)
    duration = int(settings.get("default_duration_minutes") or 30)
    start_dt = _parse_iso(slot_start)
    end_dt = _parse_iso(slot_end) or (start_dt + timedelta(minutes=duration))
    slot_end = end_dt.isoformat()
    lead_name = lead.get("name") or "Lead"
    reschedules_id = request.get("reschedules_appointment_id")
    if reschedules_id:
        _updated, err = crm_db.update_appointment(
            user_id,
            reschedules_id,
            {"start_at": slot_start, "end_at": slot_end, "status": "scheduled"},
        )
        if err:
            _revert_request(user_id, request_id, prior_status)
            return {"ok": False, "reason": err, "retryable": True}
        appt_id = reschedules_id
    else:
        appt_id, err = crm_db.create_appointment(
            user_id,
            {
                "lead_id": lead_id,
                "start_at": slot_start,
                "end_at": slot_end,
                "appointment_type": request.get("appointment_type") or "phone_call",
                "location": "",
                "notes": "Confirmed from inbound appointment request",
                "status": "scheduled",
            },
        )
        if err:
            _revert_request(user_id, request_id, prior_status)
            return {"ok": False, "reason": err, "retryable": True}

    # The qualification evaluator's verdict at booking time (read-only).
    # appointment_scheduled is OUTSIDE the qualification lane — the
    # evaluator never writes it — so the lead status goes through the
    # existing path below, not the raw-SQL write and not
    # request_status_change (which refuses out-of-lane targets).
    evaluator_note = ""
    try:
        from harness.qualification import evaluate_lead

        evaluation = evaluate_lead(user_id, lead_id)
        if evaluation:
            evaluator_note = (
                f"evaluator: {evaluation['derived_state']} "
                f"(score {evaluation['score']})"
            )
    except Exception:
        evaluator_note = ""
    crm_db.set_lead_status(
        user_id, lead_id, "appointment_scheduled", actor_user_id=user_id
    )

    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_appointment_requests
            SET status = 'confirmed', confirmed_appointment_id = ?,
                calendar_provider = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (appt_id, provider.name, _now_iso(), request_id, user_id),
        )

    # External write. A failure after the internal booking leaves the
    # booking standing in a retryable sync_failed state — never a
    # silent drop, never a rollback of a confirmed booking.
    external_event_id = None
    sync_status = "pending"
    sync_error = None
    prior_external_id = (
        _prior_external_event_id(user_id, reschedules_id) if reschedules_id else None
    )
    event = {
        "summary": f"Appointment with {lead_name}",
        "start_at": slot_start,
        "end_at": slot_end,
        "description": f"TopAI appointment with {lead_name}",
    }
    try:
        if prior_external_id:
            created = provider.update_event(
                credentials=credentials, event_id=prior_external_id, event=event
            )
        else:
            created = provider.create_event(credentials=credentials, event=event)
        external_event_id = (created or {}).get("id")
        sync_status = "synced"
    except Exception as exc:
        sync_status = "failed"
        sync_error = str(exc)[:500]
        if getattr(exc, "needs_reconnect", False):
            try:
                import calendar_connections_db as cal_db

                cal_db.mark_connection_token_expired(credentials.get("id"))
                cal_db._flag_token_expired(user_id, credentials.get("id"))
            except Exception:
                pass
        try:
            crm_db.upsert_needs_attention(
                user_id,
                lead_id,
                "appointment_sync_failed",
                priority="high",
                source_ref_type="appointment_request",
                source_ref_id=request_id,
                reason_text=f"Calendar sync failed ({sync_error}) — retry from the appointment.",
            )
        except Exception:
            pass
        _timeline(
            user_id,
            lead_id,
            "appointment_calendar_sync_failed",
            f"Calendar sync failed: {sync_error}",
            _timeline_payload(request, appointment_id=appt_id),
        )
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_appointment_requests
            SET external_event_id = ?, sync_status = ?, sync_error = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (external_event_id, sync_status, sync_error, _now_iso(), request_id, user_id),
        )

    # Confirmation text, sent by the confirm action itself (the exact
    # text the agent approved on the confirm screen). Quiet hours
    # schedules it instead of failing the confirm.
    text = build_booking_confirmation_text(slot_start, timezone_name)
    message_id = None
    text_error = None
    try:
        from sms_outbound import send_authorized_sms

        result, err, _status = send_authorized_sms(
            user_id,
            lead_id,
            text,
            source_page="appointment_request_confirm",
            compliance_confirmed=True,
            message_purpose="appointment_confirmation",
            schedule_if_quiet=True,
            cost_tool="appointment_confirmation",
        )
        if err:
            text_error = str(err)[:500]
        else:
            # send_authorized_sms returns the sms_messages row id under
            # "id" (and the provider id under "provider_message_id") —
            # there is no "message_id" key on the success payload.
            message_id = (result or {}).get("id")
    except Exception as exc:
        text_error = f"send_error: {exc}"[:500]
    if message_id:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE harness_appointment_requests SET confirm_text_message_id = ? "
                "WHERE id = ? AND user_id = ?",
                (message_id, request_id, user_id),
            )

    request = get_request(user_id, request_id)
    _audit_request(
        user_id,
        "appointment_request_confirmed",
        request_id,
        lead_id,
        "ALLOW",
        f"request #{request_id} confirmed by agent: -> event #{appt_id}, "
        f"{_slot_text(request, timezone_name)}, calendar {provider.name}",
        actor="user",
    )
    _timeline(
        user_id,
        lead_id,
        "appointment_confirmed",
        f"Appointment confirmed: {_slot_text(request, timezone_name)}",
        _timeline_payload(request, appointment_id=appt_id),
    )
    booking_reason = (
        f"event #{appt_id}: lead {lead_id}, {_slot_text(request, timezone_name)}, "
        f"{provider.name} event {external_event_id or 'sync_failed'}, "
        f"confirm text msg #{message_id or 'failed'}"
    )
    if evaluator_note:
        booking_reason += f"; {evaluator_note}"
    if text_error:
        booking_reason += f"; text failed: {text_error}"
    _audit_request(
        user_id,
        "appointment_booking_created",
        request_id,
        lead_id,
        "ALLOW",
        booking_reason,
    )
    if text_error:
        _timeline(
            user_id,
            lead_id,
            "appointment_confirmation_text_failed",
            f"Confirmation text failed: {text_error}",
            _timeline_payload(request, appointment_id=appt_id),
        )
    try:
        crm_db.resolve_needs_attention_by_reason(
            user_id, lead_id, "appointment_requested", "Appointment confirmed"
        )
    except Exception:
        pass
    return {
        "ok": True,
        "reason": "confirmed",
        "appointment_id": appt_id,
        "external_event_id": external_event_id,
        "message_id": message_id,
        "text_error": text_error,
        "retryable": False,
    }


def decline_request(user_id, request_id, *, actor="user", reason=None):
    """Decline a pending request (agent action). The lead is not texted
    automatically — the agent decides the next message. Returns
    ``(request_dict, error)``."""
    request = get_request(user_id, request_id)
    if not request:
        return None, "not_found"
    if request["status"] not in ("requested", "lead_confirmed", "conflicted"):
        return request, f"not_declinable:{request['status']}"
    _set_request_status(user_id, request_id, "declined")
    request = get_request(user_id, request_id)
    _audit_request(
        user_id,
        "appointment_request_declined",
        request_id,
        request["lead_id"],
        "REJECTED",
        f"request #{request_id} declined by agent: {_slot_text(request, None)}"
        + (f" ({reason})" if reason else ""),
        actor=actor,
    )
    _timeline(
        user_id,
        request["lead_id"],
        "appointment_declined",
        f"Appointment request declined: {_slot_text(request, None)}",
        _timeline_payload(request),
    )
    try:
        crm_db.resolve_needs_attention_by_reason(
            user_id,
            request["lead_id"],
            "appointment_requested",
            "Appointment request declined",
        )
    except Exception:
        pass
    return request, None


# ---------------------------------------------------------------------------
# Lead confirmation (the lead's "yes" — never creates the event)
# ---------------------------------------------------------------------------


def mark_lead_confirmed(user_id, request_id):
    """The lead confirmed the slot in reply to the confirmation-request
    text. Marks the request lead_confirmed; the event still waits for
    the agent tap. Returns the request, or None when not markable."""
    request = get_request(user_id, request_id)
    if not request or request["status"] != "requested":
        return None
    _set_request_status(user_id, request_id, "lead_confirmed")
    request = get_request(user_id, request_id)
    _audit_request(
        user_id,
        "appointment_lead_confirmed",
        request_id,
        request["lead_id"],
        "ALLOW",
        f"request #{request['id']}: lead confirmed {_slot_text(request, None)} via sms",
    )
    _timeline(
        user_id,
        request["lead_id"],
        "appointment_lead_confirmed",
        f"Lead confirmed the slot: {_slot_text(request, None)}",
        _timeline_payload(request),
    )
    return request


def maybe_mark_lead_confirmed(user_id, lead_id, body):
    """Mark the lead's newest pending request lead_confirmed when the
    inbound body is a bare confirmation ("yes", "sounds good", ...).
    Callers check appointment_requested first — a new booking request
    creates/attaches a request instead. Returns the request or None."""
    text = (body or "").strip().lower()
    if not text:
        return None
    if text not in _LEAD_CONFIRM_PHRASES and not _LEAD_CONFIRM_RE.match(text):
        return None
    for request in list_pending_requests(user_id, lead_id):
        if request["status"] == "requested":
            return mark_lead_confirmed(user_id, request["id"])
    return None


# ---------------------------------------------------------------------------
# Agent's own booking (Ask TopAI) — collision check + external sync (§2.7)
# ---------------------------------------------------------------------------


def check_agent_booking_collisions(user_id, start_at_iso, end_at_iso, *,
                                  exclude_appointment_id=None):
    """The same confirm-time collision check for the agent's own
    Ask TopAI booking (sources a+b+c when a calendar is connected).
    Raises :class:`CalendarUnavailableError` when the external calendar
    cannot be read."""
    return check_slot_collisions(
        user_id,
        start_at_iso,
        end_at_iso,
        exclude_appointment_id=exclude_appointment_id,
        include_external=True,
    )


def sync_agent_booking_to_calendar(user_id, appointment, *, lead_name=None):
    """Sync an agent-created appointment to the connected calendar.

    Returns ``(external_event_id, error_str)``. ``(None, None)`` when
    no calendar is connected — the agent's own booking does not require
    one (§8e). A reschedule updates the prior external event instead
    of duplicating it.
    """
    try:
        import calendar_connections_db as cal_db

        provider, credentials = cal_db.resolve_calendar_provider(user_id)
    except Exception:
        return None, None
    if provider is None:
        return None, None
    event = {
        "summary": f"Appointment with {lead_name or 'lead'}",
        "start_at": appointment.get("start_at"),
        "end_at": appointment.get("end_at"),
        "description": "",
    }
    try:
        prior = (
            _prior_external_event_id(user_id, appointment.get("id"))
            if appointment.get("id")
            else None
        )
        if prior:
            created = provider.update_event(
                credentials=credentials, event_id=prior, event=event
            )
        else:
            created = provider.create_event(credentials=credentials, event=event)
        return (created or {}).get("id"), None
    except Exception as exc:
        return None, str(exc)[:500]
