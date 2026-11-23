"""Durable duplicate-outreach ledger (Phase III-3).

One row per outreach (SMS follow-up, auto-reply, sms_reply, voice
call), keyed (tenant, lead, reason) with a channel-agnostic refusal
check: the same lead+reason refused on SMS is also refused on any
other channel that consults the ledger. The ledger is the persistent
record of the III-1 lead+reason guard, so a refusal survives
restarts, re-runs, and re-queues rather than living in job state.

The ledger never sends anything itself. Every write is tenant-scoped.
Rows older than 90 days are pruned lazily on write (one indexed
DELETE per ledger write, no scheduler); rows older than 7 days never
block, but are kept 90 days so audit/timeline references stay
resolvable.

Statuses mirror the follow-up send lifecycle: scheduled -> claimed ->
sent / drafted / blocked / dead_letter. A 'drafted' row blocks only
while its approval is still live (pending/approved/executing and not
expired); the guard looks the approval up rather than trusting the
status string alone.

No LIKE anywhere in this module (III-2 lesson): every ledger query
is equality on (user_id, lead_id, reason_norm, status).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import crm_db
import db

logger = logging.getLogger(__name__)

# Lead+reason duplicate guard (PM ruling 3, carried over from III-1):
# a second send for the same (tenant, lead, reason_norm) BLOCKs while
# an earlier send for that triple is in flight, and within this many
# days after a sent row for that triple. After the window a repeat may
# send.
DUPLICATE_WINDOW_DAYS = 7

# Ledger retention: rows older than this are pruned lazily on write.
RETENTION_DAYS = 90

# Approval statuses that mean the approval is still unsettled (a
# 'drafted' ledger row blocks only while its approval is live).
LIVE_APPROVAL_STATUSES = frozenset({"pending", "approved", "executing"})

# Pending approvals expire after this many days (harness.approvals
# EXPIRY_DAYS); an expired pending approval is not live.
APPROVAL_EXPIRY_DAYS = 7

LEDGER_COLUMNS = (
    "id",
    "user_id",
    "lead_id",
    "channel",
    "reason",
    "reason_norm",
    "dedup_key",
    "phone_normalized",
    "status",
    "sms_message_id",
    "voice_call_id",
    "job_id",
    "approval_id",
    "scheduled_at",
    "sent_at",
    "created_at",
    "updated_at",
)


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


def _dedup_key(user_id, lead_id, reason_norm):
    return f"{user_id}:{lead_id}:{reason_norm}"


def _prune_outreach_ledger(user_id, conn):
    """Delete this tenant's ledger rows older than RETENTION_DAYS.

    Called on every ledger write, in the caller's transaction when a
    connection is supplied. Best-effort: a prune failure must never
    break the outreach write itself.
    """
    cutoff = (_now() - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        conn.execute(
            "DELETE FROM harness_outreach_ledger "
            "WHERE user_id = ? AND created_at < ?",
            (user_id, cutoff),
        )
    except Exception:
        logger.warning(
            "outreach ledger prune failed tenant=%s", user_id, exc_info=True
        )


def record_outreach(
    user_id,
    *,
    lead_id,
    channel,
    reason,
    reason_norm=None,
    phone_normalized=None,
    status="scheduled",
    sms_message_id=None,
    voice_call_id=None,
    job_id=None,
    approval_id=None,
    scheduled_at=None,
    sent_at=None,
    conn=None,
):
    """Insert one ledger row. Returns the new row id.

    ``reason_norm`` defaults to the III-1 normalizer
    (``crm_db.normalize_follow_up_reason``). When ``conn`` is given the
    insert runs in the caller's transaction (and the lazy prune with
    it); otherwise it opens its own. Raises on failure — the caller
    decides whether a ledger write is fatal (follow-up paths) or
    best-effort (record-only paths, which catch it).
    """
    norm = reason_norm or crm_db.normalize_follow_up_reason(reason)
    now_iso = _now_iso()
    close_conn = conn is None

    def _insert(c):
        cur = c.execute(
            """
            INSERT INTO harness_outreach_ledger
                (user_id, lead_id, channel, reason, reason_norm, dedup_key,
                 phone_normalized, status, sms_message_id, voice_call_id,
                 job_id, approval_id, scheduled_at, sent_at, created_at,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                lead_id,
                channel,
                (reason or "Follow up")[:500],
                norm,
                _dedup_key(user_id, lead_id, norm),
                phone_normalized,
                status,
                sms_message_id,
                voice_call_id,
                job_id,
                approval_id,
                scheduled_at or now_iso,
                sent_at,
                now_iso,
                now_iso,
            ),
        )
        return cur.lastrowid

    if close_conn:
        with db.get_db() as own:
            ledger_id = _insert(own)
            _prune_outreach_ledger(user_id, own)
        return ledger_id
    ledger_id = _insert(conn)
    _prune_outreach_ledger(user_id, conn)
    return ledger_id


def get_ledger_row(ledger_id):
    with db.get_db() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM harness_outreach_ledger WHERE id = ?",
                (ledger_id,),
            ).fetchone()
        )


def get_ledger_row_by_job(user_id, job_id):
    if job_id is None:
        return None
    with db.get_db() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM harness_outreach_ledger "
                "WHERE user_id = ? AND job_id = ?",
                (user_id, job_id),
            ).fetchone()
        )


def get_ledger_row_by_approval(user_id, approval_id):
    if approval_id is None:
        return None
    with db.get_db() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM harness_outreach_ledger "
                "WHERE user_id = ? AND approval_id = ?",
                (user_id, approval_id),
            ).fetchone()
        )


def update_outreach_status(
    ledger_id,
    *,
    status,
    approval_id=None,
    job_id=None,
    scheduled_at=None,
    sent_at=None,
    conn=None,
):
    """Update status (and optional fields) on one ledger row."""
    updates = {"status": status, "updated_at": _now_iso()}
    if approval_id is not None:
        updates["approval_id"] = approval_id
    if job_id is not None:
        updates["job_id"] = job_id
    if scheduled_at is not None:
        updates["scheduled_at"] = scheduled_at
    if sent_at is not None:
        updates["sent_at"] = sent_at
    assignments = ", ".join(f"{key} = ?" for key in updates)

    def _update(c):
        c.execute(
            f"UPDATE harness_outreach_ledger SET {assignments} WHERE id = ?",
            (*updates.values(), ledger_id),
        )

    if conn is None:
        with db.get_db() as own:
            _update(own)
        return
    _update(conn)


def _approval_is_live(user_id, approval_id):
    """True when the approval behind a 'drafted' ledger row is still
    unsettled. Fails safe (True) when the approval cannot be read —
    a 'drafted' row whose approval state is unknown blocks rather
    than risking a duplicate send."""
    if approval_id is None:
        return True
    try:
        with db.get_db() as conn:
            row = _row(
                conn.execute(
                    "SELECT status, created_at FROM harness_approvals "
                    "WHERE id = ? AND user_id = ?",
                    (approval_id, user_id),
                ).fetchone()
            )
    except Exception:
        logger.warning(
            "outreach ledger approval lookup failed tenant=%s approval=%s",
            user_id,
            approval_id,
            exc_info=True,
        )
        return True
    if not row:
        return True
    if row.get("status") not in LIVE_APPROVAL_STATUSES:
        return False
    if row.get("status") == "pending":
        created = _parse_iso(row.get("created_at"))
        if created is not None and _now() >= created + timedelta(
            days=APPROVAL_EXPIRY_DAYS
        ):
            return False
    return True


def check_outreach_allowed(
    user_id, lead_id, reason_norm, *, exclude_ledger_id=None, execute_time=False
):
    """The refusal check (channel-agnostic by construction).

    Returns (allowed, conflicting_row_or_None, scope_or_None) where
    scope is "in_flight" (an earlier send for the triple is still
    scheduled/claimed, or drafted behind a live approval) or
    "within_window" (an earlier send for the triple was sent within
    DUPLICATE_WINDOW_DAYS).

    Fire time (execute_time=False): in-flight OR sent-within-window
    blocks. Approval-execute time (execute_time=True): only
    sent-within-window blocks — the first executor wins, and the
    later executor then sees the sent row and refuses.
    """
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, lead_id, channel, reason, reason_norm,
                   status, sms_message_id, voice_call_id, job_id,
                   approval_id, scheduled_at, sent_at, created_at
            FROM harness_outreach_ledger
            WHERE user_id = ? AND lead_id = ? AND reason_norm = ?
              AND status IN ('scheduled', 'claimed', 'drafted', 'sent')
            ORDER BY id ASC
            """,
            (user_id, lead_id, reason_norm),
        ).fetchall()
    window_start = _now() - timedelta(days=DUPLICATE_WINDOW_DAYS)
    for raw in rows:
        row = _row(raw)
        if exclude_ledger_id is not None and row.get("id") == exclude_ledger_id:
            continue
        status = row.get("status")
        if status in ("scheduled", "claimed"):
            if execute_time:
                continue
            return False, row, "in_flight"
        if status == "drafted":
            if not _approval_is_live(user_id, row.get("approval_id")):
                continue
            if execute_time:
                continue
            return False, row, "in_flight"
        if status == "sent":
            # Unparseable sent_at fails safe: treat as within window.
            sent_at = _parse_iso(row.get("sent_at"))
            if sent_at is None or sent_at >= window_start:
                return False, row, "within_window"
    return True, None, None


def format_prior_outreach(row):
    """Compact prior-outreach naming for the audit gate_reason and
    the timeline summary (fits the 500-char audit cap; ~120 chars)."""
    row = row or {}
    channel = row.get("channel") or "sms"
    ledger_id = row.get("id")
    reason = row.get("reason_norm") or row.get("reason") or "follow up"
    job_id = row.get("job_id")
    job_part = f"job #{job_id}, " if job_id is not None else ""
    status = row.get("status")
    if status == "sent":
        when = row.get("sent_at") or row.get("created_at") or "unknown time"
        return (
            f"prior {channel} send #{ledger_id} sent {when} "
            f"({job_part}reason '{reason}')"
        )
    if status == "drafted":
        approval_id = row.get("approval_id")
        approval_part = (
            f"approval #{approval_id}, " if approval_id is not None else ""
        )
        return (
            f"prior {channel} send #{ledger_id} drafted "
            f"({approval_part}{job_part}reason '{reason}')"
        )
    when = row.get("scheduled_at") or row.get("created_at") or "unknown time"
    return (
        f"prior {channel} send #{ledger_id} scheduled {when} "
        f"({job_part}reason '{reason}')"
    )


def write_duplicate_timeline_entry(user_id, lead_id, conflicting_row):
    """Lead-timeline entry naming the prior outreach (the visible half
    of a duplicate refusal). Best-effort: never raises."""
    prior_text = format_prior_outreach(conflicting_row)
    try:
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "duplicate_outreach_refused",
            f"Duplicate outreach refused: {prior_text}",
            {
                "prior_ledger_id": (conflicting_row or {}).get("id"),
                "prior_channel": (conflicting_row or {}).get("channel"),
                "prior_status": (conflicting_row or {}).get("status"),
                "prior_sent_at": (conflicting_row or {}).get("sent_at"),
                "prior_job_id": (conflicting_row or {}).get("job_id"),
                "prior_reason_norm": (conflicting_row or {}).get("reason_norm"),
                "scope": "in_flight"
                if (conflicting_row or {}).get("status") != "sent"
                else "within_window",
            },
        )
    except Exception:
        logger.warning(
            "duplicate-refusal timeline write failed tenant=%s lead=%s",
            user_id,
            lead_id,
            exc_info=True,
        )


def write_duplicate_refusal_evidence(
    user_id,
    lead_id,
    conflicting_row,
    *,
    job_id=None,
    correlation_id=None,
    tool="follow_up_sms_send",
    action="follow_up_sms_blocked",
    mode=None,
):
    """Audit row + lead-timeline entry naming the prior outreach.

    The audit gate_reason and the timeline summary carry the same
    prior-outreach naming; the timeline payload carries the prior
    ledger row's ids so the refusal is resolvable after the 90-day
    prune. Best-effort: an evidence-write failure is logged, never
    raised — the refusal itself has already been decided.
    """
    prior_text = format_prior_outreach(conflicting_row)
    gate_reason = f"duplicate_follow_up_send: {prior_text}"[:500]
    try:
        from harness.audit import record_audit

        record_audit(
            user_id,
            "ai",
            action,
            tool=tool,
            target_type="lead",
            target_id=lead_id,
            mode=mode,
            gate_decision="BLOCK",
            gate_reason=gate_reason,
            cost_cents=0,
            request_id=str(job_id) if job_id is not None else None,
            correlation_id=correlation_id
            or (str(job_id) if job_id is not None else None),
        )
    except Exception:
        logger.warning(
            "duplicate-refusal audit write failed tenant=%s lead=%s",
            user_id,
            lead_id,
            exc_info=True,
        )
    write_duplicate_timeline_entry(user_id, lead_id, conflicting_row)
    return gate_reason


def list_outreach_for_lead(user_id, lead_id):
    """Serialized ledger rows for one lead (the §8.1 read surface)."""
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, channel, reason, reason_norm, status,
                   sms_message_id, voice_call_id, job_id, approval_id,
                   scheduled_at, sent_at, created_at, updated_at
            FROM harness_outreach_ledger
            WHERE user_id = ? AND lead_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id, lead_id),
        ).fetchall()
    return [_row(r) for r in rows]


def find_phone_conflicts(conn):
    """(user_id, phone_normalized, lead_ids) groups with >1 lead.

    The staged-UNIQUE gate: the follow-up UNIQUE on
    leads.phone_normalized lands only once this reports zero
    conflicts in staging + production. Defensive row reading: the
    empty-Postgres test fake stubs COUNT(*) queries with a bare
    ``{"c": 0}`` row, which this skips.
    """
    try:
        rows = conn.execute(
            """
            SELECT user_id, phone_normalized, COUNT(*) AS n
            FROM leads
            WHERE phone_normalized IS NOT NULL AND phone_normalized != ''
            GROUP BY user_id, phone_normalized
            HAVING COUNT(*) > 1
            """
        ).fetchall()
    except Exception:
        logger.warning("phone conflict report failed", exc_info=True)
        return []
    conflicts = []
    for raw in rows:
        try:
            row = dict(raw)
        except (TypeError, ValueError):
            continue
        if row.get("user_id") is None or not row.get("phone_normalized"):
            continue
        conflicts.append(
            {
                "user_id": row["user_id"],
                "phone_normalized": row["phone_normalized"],
                "count": row.get("n"),
            }
        )
    return conflicts
