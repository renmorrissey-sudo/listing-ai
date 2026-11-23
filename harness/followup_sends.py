"""Gated follow-up SMS sending + scheduling (Phase III-1).

Two entry points:

* :func:`schedule_follow_up_sms` — schedule a follow-up send. Validates
  the lead, enforces the per-tenant / per-lead pending caps and the
  schedule-time messaging-consent check, creates the outbound
  ``sms_messages`` draft row (the body lives ONLY there, never in the
  job payload), inserts the ``harness_follow_up_sends`` ledger row, and
  enqueues a ``follow_up_sms_send`` job with ``next_attempt_at`` set to
  the scheduled fire time. The text is caller-supplied (PM ruling 2):
  stored verbatim, sent verbatim, no model call, no new spend.
* :func:`process_follow_up_sms_send_job` — the worker handler for a
  ``follow_up_sms_send`` job. Re-checks every gate input fresh at fire
  time and routes ALLOW / DRAFT / BLOCK / DEFER exactly as the
  auto-reply and manual-send handlers do, with the follow-up-specific
  differences: quiet hours RESCHEDULES (never drops) by enqueueing a
  continuation job for the next permitted time and re-pointing the
  ledger row at it (the worker settles every ``ok: true`` result via
  ``complete_job``, so an in-place reschedule of the same job row
  cannot survive the handler's return — see
  :func:`_handle_quiet_hours_defer`); the lead+reason duplicate guard
  (PM ruling 3) BLOCKs a second send for the same (tenant, lead,
  reason) while an earlier send is in flight or within 7 days of a
  sent row; and the outbound row is claimed atomically before the
  provider call (layer 4 of the double-fire guards) so a crash between
  provider-accept and job completion can never double-send.

Result keys mirror ``harness.sms_handler``: ok, sent, decision,
reason, approval_id, retryable. ``ok=False, retryable=False`` means a
permanent failure (worker dead-letters); ``ok=False, retryable=True``
means a transient failure worth retrying with backoff.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import config
import crm_db
import db

logger = logging.getLogger(__name__)

# Caps (PM ruling 6): max pending scheduled follow-up sends per tenant
# and per lead, enforced at schedule time.
MAX_PENDING_PER_TENANT = 50
MAX_PENDING_PER_LEAD = 5

# Quiet-hours deferral cap (PM default): after this many nightly
# deferrals the send dead-letters instead of looping forever.
QUIET_HOURS_DEFERRAL_CAP = 14

# Lead+reason duplicate guard (PM ruling 3), ledger-backed since
# Phase III-3: see harness/outreach_ledger.py. A second send for the
# same (tenant, lead, reason_norm) BLOCKs while an earlier send for
# that triple is in flight, and within DUPLICATE_WINDOW_DAYS after a
# sent row for that triple. After the window a repeat may send.
# (DUPLICATE_WINDOW_DAYS lives in harness.outreach_ledger.)

# sms_messages statuses that mean the provider already accepted the
# message (mock provider records "mock_sent").
_SENT_STATUSES = frozenset({"sent", "delivered", "queued", "mock_sent"})

# Substrings in a send_authorized_sms error that mark a deterministic
# (never-retryable) decline. Anything else is treated as transient.
_DETERMINISTIC_SEND_ERROR_MARKERS = (
    "opted out",
    "opt-out",
    "suppression",
    "consent",
    "certif",
    "not permitted",
    "revoked",
    "invalid number",
    "trial destination",
    "toll-free",
    "not verified",
    "blocked",
)


class FollowUpScheduleError(ValueError):
    """Scheduling refused (caps, consent, bad input). ``code`` is stable
    and machine-readable; ``str(exc)`` is the human-readable message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _row(row):
    return dict(row) if row is not None else None


def _parse_iso_datetime(value, code, label):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise FollowUpScheduleError(
                code, f"{label} is not a valid ISO-8601 datetime: {value!r}"
            )
    else:
        raise FollowUpScheduleError(
            code, f"{label} must be an ISO-8601 datetime string"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reason_slug(reason):
    slug = re.sub(r"[^a-z0-9]+", "-", (reason or "").strip().lower()).strip("-")
    return slug or "follow-up"


# --- Messaging-consent evidence (PM ruling 10) ---------------------------
#
# Schedule-time rule (exact, from the code):
#   has evidence  <=>  lead.sms_consent_status (lower-cased) is in
#   crm_constants.SMS_CONSENT_CERTIFIED_STATUSES {"verified",
#   "user_certified"} — the two values sms_authorization._certified_status
#   treats as certified — OR a sms_consent_evidence row exists for
#   (user_id, lead_id) with consent_status='confirmed' and
#   revoked_at IS NULL (external-leads evidence store).
# Anything else — "unverified", "not_certified", NULL/empty, or a
# blocked state — is NO evidence. Blocked states (opted_out, revoked,
# not_permitted, suppressed, invalid_number) are also caught by the
# fire-time policy gate, but the schedule-time check refuses before
# anything is enqueued, so a send can never be scheduled for a lead
# with no consent evidence at all.


def has_messaging_consent_evidence(user_id, lead):
    """True when the lead has messaging-consent evidence (see above)."""
    if not lead:
        return False
    try:
        from crm_constants import SMS_CONSENT_CERTIFIED_STATUSES

        certified = set(SMS_CONSENT_CERTIFIED_STATUSES)
    except Exception:
        certified = {"verified", "user_certified"}
    status = (lead.get("sms_consent_status") or "").strip().lower()
    if status in certified:
        return True
    lead_id = lead.get("id")
    if not lead_id:
        return False
    try:
        import external_leads_db as xdb

        rows = xdb.list_consent_evidence(user_id, lead_id, limit=50)
    except Exception:
        return False
    for row in rows:
        if (row.get("consent_status") or "").strip().lower() == "confirmed" and not row.get(
            "revoked_at"
        ):
            return True
    return False


# --- Scheduling ------------------------------------------------------------


def _existing_job_for_key(user_id, idempotency_key):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM harness_jobs WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key),
        ).fetchone()
    return _row(row)


def _get_send_row_by_job(user_id, job_id):
    with db.get_db() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM harness_follow_up_sends WHERE user_id = ? AND job_id = ?",
                (user_id, job_id),
            ).fetchone()
        )


def _pending_counts(user_id, lead_id):
    with db.get_db() as conn:
        tenant_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM harness_follow_up_sends
            WHERE user_id = ? AND status IN ('scheduled', 'claimed')
            """,
            (user_id,),
        ).fetchone()
        lead_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM harness_follow_up_sends
            WHERE user_id = ? AND lead_id = ? AND status IN ('scheduled', 'claimed')
            """,
            (user_id, lead_id),
        ).fetchone()
    return int(tenant_row["n"]), int(lead_row["n"])


def schedule_follow_up_sms(user_id, lead_id, follow_up_id, body, scheduled_at, reason):
    """Schedule a follow-up SMS send. Returns a result dict.

    Validates the lead, the body (non-empty, <= 2000 chars), and the
    scheduled time; refuses (``FollowUpScheduleError``, nothing
    enqueued, no rows created) when the lead has no messaging-consent
    evidence or a pending cap is hit. A ``scheduled_at`` in the past is
    clamped to now — the returned ``scheduled_at`` is the effective
    fire time, so the caller always sees that the send is due
    immediately rather than silently firing later.

    Idempotent per (tenant, lead, reason, scheduled minute): a second
    call with the same triple returns the original job id and creates
    no new rows.
    """
    from harness.validation import ValidationError, validate_lead_reference

    lead = validate_lead_reference(user_id, lead_id, required=True)
    lead_id = lead["id"]

    text = (body or "").strip() if isinstance(body, str) else ""
    if not text:
        raise FollowUpScheduleError("invalid_body", "message body is required")
    if len(text) > 2000:
        raise FollowUpScheduleError(
            "invalid_body", "message body must be 2000 characters or fewer"
        )

    reason = (reason or "Follow up").strip()[:500] or "Follow up"
    reason_norm = crm_db.normalize_follow_up_reason(reason)

    requested_at = _parse_iso_datetime(
        scheduled_at, "invalid_scheduled_at", "scheduled_at"
    )
    now = _now()
    clamped = requested_at < now
    effective_at = now if clamped else requested_at
    effective_iso = effective_at.isoformat()
    scheduled_minute = effective_at.replace(second=0, microsecond=0).isoformat()

    # Follow-up link: when given, the CRM row must exist for this
    # tenant/lead; when omitted the send is standalone (no CRM row is
    # auto-created — the coach's follow-up suggestion stays a CRM
    # record of its own).
    follow_up_id_int = None
    if follow_up_id not in (None, ""):
        try:
            follow_up_id_int = int(follow_up_id)
        except (TypeError, ValueError):
            raise FollowUpScheduleError(
                "malformed_follow_up_id",
                f"follow_up_id must be an integer, got {follow_up_id!r}",
            )
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT id FROM lead_follow_ups WHERE id = ? AND user_id = ? AND lead_id = ?",
                (follow_up_id_int, user_id, lead_id),
            ).fetchone()
        if row is None:
            raise FollowUpScheduleError(
                "follow_up_not_found",
                f"follow-up {follow_up_id_int} does not exist for this lead",
            )

    # Schedule-time consent check (ruling 10): refuse before anything
    # is enqueued or created when the lead has no messaging-consent
    # evidence. The fire-time gate re-checks fresh (revocation after
    # scheduling BLOCKs then).
    if not has_messaging_consent_evidence(user_id, lead):
        raise FollowUpScheduleError(
            "consent_missing",
            "cannot schedule a follow-up SMS: this lead has no "
            "messaging-consent evidence (consent status is not certified "
            "and no confirmed consent evidence is on file)",
        )

    # Pending caps (ruling 6): 50 scheduled sends per tenant, 5 per lead.
    tenant_pending, lead_pending = _pending_counts(user_id, lead_id)
    if tenant_pending >= MAX_PENDING_PER_TENANT:
        raise FollowUpScheduleError(
            "pending_cap_reached",
            f"tenant already has {tenant_pending} pending scheduled "
            f"follow-up sends (max {MAX_PENDING_PER_TENANT})",
        )
    if lead_pending >= MAX_PENDING_PER_LEAD:
        raise FollowUpScheduleError(
            "per_lead_cap_reached",
            f"lead already has {lead_pending} pending scheduled "
            f"follow-up sends (max {MAX_PENDING_PER_LEAD})",
        )

    idempotency_key = (
        f"follow-up-sms-{user_id}-{lead_id}-"
        f"{_reason_slug(reason)}-{scheduled_minute}"
    )

    # Scheduling-time dedup (layer 1): same key => same job, no new rows.
    existing = _existing_job_for_key(user_id, idempotency_key)
    if existing is not None:
        send_row = _get_send_row_by_job(user_id, existing["id"])
        return {
            "job_id": existing["id"],
            "follow_up_send_id": send_row["id"] if send_row else None,
            "message_id": send_row["sms_message_id"] if send_row else None,
            "scheduled_at": send_row["scheduled_at"] if send_row else effective_iso,
            "follow_up_id": follow_up_id_int,
            "duplicate": True,
        }

    # Pre-create the outbound row (status draft, consent confirmed):
    # the body lives here and is loaded by id at fire time, never in
    # the job payload.
    phone = (lead.get("phone_number") or "").strip()
    message_id = db.create_sms_message(
        user_id,
        None,
        config.SMS_PROVIDER,
        {
            "lead_name": lead.get("name"),
            "phone_number": phone,
            "lead_type": lead.get("lead_type"),
            "property_interest": lead.get("property_interest"),
            "message_body": text,
        },
        status="draft",
        lead_id=lead_id,
        direction="outbound",
        consent_status="confirmed",
        opt_out_status=lead.get("opt_out_status") or "active",
    )

    from harness.jobs import enqueue_job

    job_id = enqueue_job(
        user_id,
        "follow_up_sms_send",
        {
            "lead_id": lead_id,
            "follow_up_id": follow_up_id_int,
            "reason": reason,
            "scheduled_at": effective_iso,
            "message_id": message_id,
        },
        idempotency_key=idempotency_key,
        next_attempt_at=effective_iso,
    )

    # Lost an enqueue race with a duplicate scheduler: the key lookup
    # now returns the pre-existing job. Drop our orphan draft row and
    # return the original job (no second send row, no second job).
    prior_send_row = _get_send_row_by_job(user_id, job_id)
    if prior_send_row is not None:
        with db.get_db() as conn:
            conn.execute(
                "DELETE FROM sms_messages WHERE id = ? AND user_id = ? AND status = 'draft'",
                (message_id, user_id),
            )
        return {
            "job_id": job_id,
            "follow_up_send_id": prior_send_row["id"],
            "message_id": prior_send_row["sms_message_id"],
            "scheduled_at": prior_send_row["scheduled_at"],
            "follow_up_id": follow_up_id_int,
            "duplicate": True,
        }

    now_iso = _now_iso()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_follow_up_sends
                (user_id, lead_id, follow_up_id, job_id, reason, reason_norm,
                 sms_message_id, status, scheduled_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)
            """,
            (
                user_id,
                lead_id,
                follow_up_id_int,
                job_id,
                reason,
                reason_norm,
                message_id,
                effective_iso,
                now_iso,
                now_iso,
            ),
        )
        send_id = cur.lastrowid
        # Phase III-3: the durable outreach ledger row for this send,
        # created in the same transaction as the send row so the two
        # can never diverge at schedule time.
        from harness.outreach_ledger import record_outreach
        from lead_service import normalize_phone_e164

        record_outreach(
            user_id,
            lead_id=lead_id,
            channel="sms",
            reason=reason,
            reason_norm=reason_norm,
            phone_normalized=normalize_phone_e164(phone),
            status="scheduled",
            sms_message_id=message_id,
            job_id=job_id,
            scheduled_at=effective_iso,
            conn=conn,
        )
    return {
        "job_id": job_id,
        "follow_up_send_id": send_id,
        "message_id": message_id,
        "scheduled_at": effective_iso,
        "requested_scheduled_at": requested_at.isoformat(),
        "clamped_to_now": clamped,
        "follow_up_id": follow_up_id_int,
        "duplicate": False,
    }


# --- Worker handler ----------------------------------------------------------


def _audit(user_id, *, action, gate_decision, gate_reason, mode=None,
           target_id=None, job_id=None, correlation_id=None):
    """Append one harness_audit row (tool=follow_up_sms_send). Best-effort."""
    try:
        from harness.audit import record_audit

        record_audit(
            user_id,
            "ai",
            action,
            tool="follow_up_sms_send",
            target_type="lead",
            target_id=target_id,
            mode=mode,
            gate_decision=gate_decision,
            gate_reason=(gate_reason or "")[:500],
            cost_cents=0,
            request_id=str(job_id) if job_id is not None else None,
            correlation_id=correlation_id or (str(job_id) if job_id is not None else None),
        )
    except Exception:
        logger.warning(
            "follow_up_sms audit write failed tenant=%s job=%s action=%s",
            user_id,
            job_id,
            action,
        )


def _needs_attention(user_id, lead_id, message_id, reason_code):
    """Create a needs-attention entry via the existing CRM helper."""
    try:
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            reason_code,
            priority="high",
            source_ref_type="sms",
            source_ref_id=message_id,
        )
    except Exception:
        logger.exception(
            "Failed to flag needs attention lead_id=%s tenant=%s", lead_id, user_id
        )


def _is_rate_limited(user_id, phone):
    """Real tenant/contact rate check; fail closed on any check error."""
    try:
        from sms_authorization import _rate_limited

        return bool(_rate_limited(user_id, phone))
    except Exception:
        logger.exception("rate-limit check failed tenant=%s; failing closed", user_id)
        return True


def _payload_dict(job):
    payload = (job or {}).get("payload")
    if payload is None:
        payload = (job or {}).get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _update_send_row(send_id, **fields):
    """Update allowed columns on a harness_follow_up_sends row."""
    allowed = {
        "status", "job_id", "sent_at", "provider_message_id", "approval_id",
        "deferred_count", "last_gate_reason", "scheduled_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = _now_iso()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with db.get_db() as conn:
        conn.execute(
            f"UPDATE harness_follow_up_sends SET {assignments} WHERE id = ?",
            (*updates.values(), send_id),
        )


def _set_sms_row_draft(message_id, error_message=None):
    """Return a claimed outbound row to draft (e.g. after a BLOCK)."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE sms_messages SET status = 'draft', error_message = ? WHERE id = ?",
            (error_message, message_id),
        )


def _claim_outbound_row(message_id):
    """Atomically claim the pre-created outbound row for sending.

    Returns (state, row) with state in {"claimed", "already_sent",
    "sending", "unclaimable"}:

    * ``claimed`` — this caller won the draft/failed -> sending
      transition and owns the provider call.
    * ``already_sent`` — the row is already sent/delivered/queued or
      carries a provider id: the send already happened; never call
      the provider again (layer 4).
    * ``sending`` — the row is mid-send from an earlier attempt of
      this same job (crash between claim and provider call, or a
      retry after a transient failure): the caller may proceed, since
      the job claim guarantees no concurrent worker holds it.
    * ``unclaimable`` — cancelled or otherwise terminal; fail the job.
    """
    with db.get_db() as conn:
        cur = conn.execute(
            """
            UPDATE sms_messages
            SET status = 'sending', error_message = NULL
            WHERE id = ? AND status IN ('draft', 'failed')
            """,
            (message_id,),
        )
        if cur.rowcount == 1:
            row = conn.execute(
                "SELECT * FROM sms_messages WHERE id = ?", (message_id,)
            ).fetchone()
            return "claimed", _row(row)
    with db.get_db() as conn:
        row = _row(
            conn.execute(
                "SELECT * FROM sms_messages WHERE id = ?", (message_id,)
            ).fetchone()
        )
    if not row:
        return "unclaimable", None
    if row.get("status") in _SENT_STATUSES or row.get("provider_message_id"):
        return "already_sent", row
    if row.get("status") == "sending":
        return "sending", row
    return "unclaimable", row


def _set_ledger_status(user_id, lookup_job_id, status, **fields):
    """Mirror a lifecycle transition onto the outreach ledger row for
    this job (Phase III-3). No-op with a warning when the row is
    missing (a pre-047 job the backfill did not cover) — the send
    itself must never fail for a missing ledger row."""
    try:
        from harness.outreach_ledger import (
            get_ledger_row_by_job,
            update_outreach_status,
        )

        row = get_ledger_row_by_job(user_id, lookup_job_id)
        if row is None:
            logger.warning(
                "outreach ledger row missing job=%s tenant=%s status=%s",
                lookup_job_id,
                user_id,
                status,
            )
            return
        update_outreach_status(row["id"], status=status, **fields)
    except Exception:
        logger.warning(
            "outreach ledger status update failed job=%s tenant=%s status=%s",
            lookup_job_id,
            user_id,
            status,
            exc_info=True,
        )


def _decide_follow_up_policy(user_id, lead, mode, paused, correlation_id=None):
    """Ask harness.policy for the follow-up send decision.

    Mirrors ``harness.sms_handler._decide_policy`` / ``_decide_manual_policy``,
    with the two fields neither existing handler passes:

    * ``do_not_contact`` — ``lead.status == "do_not_contact"`` (the
      policy checks it, but no SMS handler ever set it, so the check
      was dead for SMS jobs; this is the regression fix).
    * ``consent_missing`` — no messaging-consent evidence for the lead
      (see :func:`has_messaging_consent_evidence`).

    Returns (decision, reason, rate_limited, quiet_hours).
    """
    if paused:
        return "DRAFT", "paused", False, False
    if mode == "drafts_only":
        return "DRAFT", "mode_drafts_only", False, False
    try:
        from harness.policy import decide
    except Exception:
        logger.warning("harness.policy unavailable; defaulting to DRAFT tenant=%s", user_id)
        return "DRAFT", "policy_unavailable", False, False
    phone = (lead or {}).get("phone_number") or ""
    suppressed = False
    try:
        import tenant_sms_db as tdb

        suppressed = bool(tdb.is_suppressed(user_id, phone))
    except Exception:
        suppressed = False
    quiet_hours = False
    try:
        from sms_quiet_hours import in_quiet_hours

        quiet_hours = bool(in_quiet_hours(user_id, phone=phone, lead=lead))
    except Exception:
        quiet_hours = False
    rate_limited = _is_rate_limited(user_id, phone)
    human_takeover = False
    try:
        from harness.inbox import is_human_takeover

        lead_id = (lead or {}).get("id")
        if lead_id:
            human_takeover = bool(is_human_takeover(user_id, lead_id))
    except Exception:
        human_takeover = False
    context = {
        "mode": mode,
        "paused": paused,
        "human_takeover": human_takeover,
        "consent_status": (lead or {}).get("sms_consent_status") or "",
        "opted_out": (lead or {}).get("opt_out_status") == "opted_out",
        "suppressed": suppressed,
        "do_not_contact": (lead or {}).get("status") == "do_not_contact",
        "consent_missing": not has_messaging_consent_evidence(user_id, lead),
        "quiet_hours": quiet_hours,
        "rate_limited": rate_limited,
        "escalation_topics": [],
        "template_approved": False,
        "correlation_id": correlation_id,
    }
    try:
        result = decide(user_id, "send_sms", "send_sms", context, correlation_id=correlation_id)
    except Exception:
        logger.exception("harness.policy.decide failed tenant=%s", user_id)
        return "DRAFT", "policy_error", rate_limited, quiet_hours
    if isinstance(result, (tuple, list)) and len(result) == 2:
        decision, reason = result
        decision = str(decision or "DRAFT").upper()
        reason = str(reason or "")[:500]
    elif isinstance(result, dict):
        decision = str(result.get("decision") or result.get("gate_decision") or "DRAFT").upper()
        reason = str(result.get("reason") or result.get("gate_reason") or "")[:500]
    else:
        decision = str(result or "DRAFT").upper()
        reason = ""
    if decision not in {"ALLOW", "DRAFT", "BLOCK", "DEFER"}:
        return "DRAFT", "policy_unknown_decision", rate_limited, quiet_hours
    return decision, reason or "policy_decision", rate_limited, quiet_hours


def _create_approval(user_id, job_id, kind, draft_payload):
    """Insert a pending harness_approvals row. Returns id or None."""
    try:
        with db.get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO harness_approvals
                    (user_id, job_id, kind, draft_json, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (user_id, job_id, kind, json.dumps(draft_payload), _now_iso()),
            )
            return cur.lastrowid
    except Exception:
        logger.warning(
            "harness_approvals write failed tenant=%s job=%s (table may not exist yet)",
            user_id,
            job_id,
        )
        return None


def _complete_send_side_effects(user_id, lead_id, follow_up_id, send_row, sms_row):
    """Settle the ledger + CRM record after a confirmed send.

    Marks the send row sent, completes the CRM follow-up via the
    existing crm_db path (never a raw UPDATE), touches the lead's
    outbound stamp, and writes the lead-activity row. Idempotent: safe
    to call on the already_sent path too. Mirrors the transition onto
    the outreach ledger row (Phase III-3) so the guard sees the send.
    """
    now_iso = _now_iso()
    _update_send_row(
        send_row["id"],
        status="sent",
        sent_at=sms_row.get("sent_at") or now_iso,
        provider_message_id=sms_row.get("provider_message_id"),
        last_gate_reason="sent",
    )
    _set_ledger_status(
        user_id,
        send_row.get("job_id"),
        "sent",
        sent_at=sms_row.get("sent_at") or now_iso,
    )
    if follow_up_id:
        try:
            crm_db.complete_lead_follow_up(user_id, lead_id, follow_up_id)
        except Exception:
            logger.exception(
                "complete_lead_follow_up failed tenant=%s lead=%s follow_up=%s",
                user_id,
                lead_id,
                follow_up_id,
            )
    try:
        db.touch_lead_outbound(lead_id, user_id)
    except Exception:
        logger.exception("touch_lead_outbound failed lead=%s tenant=%s", lead_id, user_id)


def _handle_draft(user_id, lead_id, job_id, send_row, sms_row, body, mode,
                  gate_reason, follow_up_id, scheduled_at, correlation_id):
    """DRAFT outcome: pending approval, no send, job settled."""
    approval_id = _create_approval(
        user_id,
        job_id,
        "follow_up_sms",
        {
            "draft_reply": body,
            "lead_id": lead_id,
            "follow_up_id": follow_up_id,
            "job_id": job_id,
            "follow_up_send_id": send_row["id"],
            "message_id": sms_row["id"],
            "gate_reason": gate_reason,
            "scheduled_at": scheduled_at,
        },
    )
    _update_send_row(
        send_row["id"], status="drafted", approval_id=approval_id,
        last_gate_reason=gate_reason,
    )
    _set_ledger_status(
        user_id, job_id, "drafted", approval_id=approval_id,
    )
    _audit(
        user_id,
        action="follow_up_sms_drafted",
        gate_decision="DRAFT",
        gate_reason=gate_reason,
        mode=mode,
        target_id=lead_id,
        job_id=job_id,
        correlation_id=correlation_id,
    )
    logger.info(
        "follow_up_sms drafted (no send) job=%s tenant=%s lead=%s approval=%s",
        job_id,
        user_id,
        lead_id,
        approval_id,
    )
    return {
        "ok": True,
        "sent": False,
        "decision": "DRAFT",
        "reason": gate_reason,
        "approval_id": approval_id,
        "retryable": False,
    }


def _handle_block(user_id, lead_id, job_id, send_row, sms_row, mode,
                  gate_reason, correlation_id, *, reset_sms_row=False,
                  conflicting_row=None):
    """BLOCK outcome: audit + needs-attention, no send, no approval.

    The CRM follow-up row stays pending (a human may still do the
    follow-up manually). The draft sms row stays a draft so the text
    remains usable by a human.

    When ``conflicting_row`` is given (the duplicate guard fired), the
    audit row + a lead-timeline entry name the prior outreach
    (Phase III-3, criterion 2) instead of the bare gate reason, and
    the ledger row for the blocked attempt is recorded 'blocked'.
    The result reason stays the short gate_reason so existing callers
    (and the III-1 test) keep seeing "duplicate_follow_up_send".
    """
    _update_send_row(
        send_row["id"], status="blocked", last_gate_reason=gate_reason,
    )
    _set_ledger_status(user_id, job_id, "blocked")
    if reset_sms_row and sms_row is not None:
        _set_sms_row_draft(sms_row["id"], error_message=gate_reason)
    if conflicting_row is not None:
        from harness.outreach_ledger import write_duplicate_refusal_evidence

        write_duplicate_refusal_evidence(
            user_id,
            lead_id,
            conflicting_row,
            job_id=job_id,
            correlation_id=correlation_id,
            mode=mode,
        )
    else:
        _audit(
            user_id,
            action="follow_up_sms_blocked",
            gate_decision="BLOCK",
            gate_reason=gate_reason,
            mode=mode,
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
        )
    _needs_attention(user_id, lead_id, sms_row["id"] if sms_row else None,
                     "follow_up_sms_blocked")
    logger.info(
        "follow_up_sms BLOCKED (no send) job=%s tenant=%s lead=%s reason=%s",
        job_id,
        user_id,
        lead_id,
        gate_reason,
    )
    return {
        "ok": True,
        "sent": False,
        "decision": "BLOCK",
        "reason": gate_reason,
        "approval_id": None,
        "retryable": False,
    }


def _handle_quiet_hours_defer(user_id, lead, job_id, send_row, sms_row, mode,
                              gate_reason, correlation_id):
    """DEFER (quiet hours): reschedule to the next permitted time.

    The job is settled for this firing; the rescheduled firing is a
    CONTINUATION job, not a re-claim of the same job row. Why: the
    worker settles every ``ok: true`` handler result via
    ``complete_job`` (workers/harness_worker.py ``_run_known_handler``,
    harness/jobs.py ``complete_job`` sets status='succeeded'
    unconditionally), so an in-place ``reschedule_job`` on the current
    row inside the handler would be overwritten the moment the handler
    returns — the sketch's same-row reschedule cannot work in this
    codebase. Instead the handler enqueues a fresh ``follow_up_sms_send``
    job for the rescheduled time (idempotency key suffixed with the
    deferral count, so it never collides with the original key even
    when the rescheduled minute equals the original one) and re-points
    the ledger row at it. The send row stays ``scheduled`` with the new
    fire time and ``deferred_count += 1``; the sms row keeps its draft
    status. No retry budget is burned and no exponential backoff is
    taken. After QUIET_HOURS_DEFERRAL_CAP deferrals the send
    dead-letters instead of looping forever.
    """
    from sms_quiet_hours import next_permitted_send_at

    phone = (lead or {}).get("phone_number") or ""
    try:
        send_at = next_permitted_send_at(user_id, phone=phone, lead=lead)
    except Exception:
        logger.exception("next_permitted_send_at failed tenant=%s", user_id)
        send_at = _now() + timedelta(hours=1)
    now = _now()
    if send_at.tzinfo is None:
        send_at = send_at.replace(tzinfo=timezone.utc)
    if send_at <= now:
        # Never reschedule into the past (clock skew / misconfigured
        # window): clamp to one minute out and log.
        send_at = now + timedelta(seconds=60)
    deferred_count = int(send_row.get("deferred_count") or 0)
    if deferred_count >= QUIET_HOURS_DEFERRAL_CAP:
        _update_send_row(
            send_row["id"], status="dead_letter",
            last_gate_reason="quiet_hours_deferral_cap_reached",
        )
        _set_ledger_status(user_id, job_id, "dead_letter")
        _audit(
            user_id,
            action="follow_up_sms_deferred",
            gate_decision="DEFER",
            gate_reason="quiet_hours_deferral_cap_reached",
            mode=mode,
            target_id=lead.get("id"),
            job_id=job_id,
            correlation_id=correlation_id,
        )
        _needs_attention(
            user_id, lead.get("id"),
            sms_row["id"] if sms_row else None, "follow_up_sms_deferred",
        )
        return {
            "ok": False,
            "sent": False,
            "decision": "DEFER",
            "reason": "quiet_hours_deferral_cap_reached",
            "approval_id": None,
            "retryable": False,
        }
    send_at_iso = send_at.isoformat()
    # Continuation job for the rescheduled firing (see docstring).
    from harness.jobs import enqueue_job

    new_count = deferred_count + 1
    scheduled_minute = send_at.replace(second=0, microsecond=0).isoformat()
    continuation_key = (
        f"follow-up-sms-{user_id}-{lead.get('id')}-"
        f"{_reason_slug(send_row.get('reason') or 'Follow up')}-"
        f"{scheduled_minute}-defer-{new_count}"
    )
    continuation_job_id = enqueue_job(
        user_id,
        "follow_up_sms_send",
        {
            "lead_id": lead.get("id"),
            "follow_up_id": send_row.get("follow_up_id"),
            "reason": send_row.get("reason") or "Follow up",
            "scheduled_at": send_at_iso,
            "message_id": sms_row["id"],
        },
        idempotency_key=continuation_key,
        next_attempt_at=send_at_iso,
    )
    _update_send_row(
        send_row["id"],
        status="scheduled",
        job_id=continuation_job_id,
        scheduled_at=send_at_iso,
        deferred_count=new_count,
        last_gate_reason=f"quiet_hours; rescheduled_for={send_at_iso}",
    )
    # The ledger row stays 'scheduled' with the new fire time and is
    # re-pointed at the continuation job (Phase III-3).
    _set_ledger_status(
        user_id,
        job_id,
        "scheduled",
        job_id=continuation_job_id,
        scheduled_at=send_at_iso,
    )
    # Keep the sms-row view consistent with the new fire time without
    # changing its draft status (db.schedule_sms_message would flip it
    # to 'scheduled'; the harness job is the scheduler here).
    try:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE sms_messages SET scheduled_send_at = ? WHERE id = ?",
                (send_at_iso, sms_row["id"]),
            )
    except Exception:
        logger.exception("sms scheduled_send_at echo failed message=%s", sms_row["id"])
    _audit(
        user_id,
        action="follow_up_sms_deferred",
        gate_decision="DEFER",
        gate_reason=f"quiet_hours; rescheduled_for={send_at_iso}",
        mode=mode,
        target_id=lead.get("id"),
        job_id=job_id,
        correlation_id=correlation_id,
    )
    _needs_attention(
        user_id, lead.get("id"),
        sms_row["id"] if sms_row else None, "follow_up_sms_deferred",
    )
    logger.info(
        "follow_up_sms DEFERRED quiet hours (rescheduled) job=%s tenant=%s lead=%s for=%s continuation_job=%s",
        job_id,
        user_id,
        lead.get("id"),
        send_at_iso,
        continuation_job_id,
    )
    return {
        "ok": True,
        "sent": False,
        "decision": "DEFER",
        "reason": "quiet_hours",
        "approval_id": None,
        "retryable": False,
        "rescheduled_for": send_at_iso,
        "rescheduled_job_id": continuation_job_id,
    }


def process_follow_up_sms_send_job(job):
    """Handle one ``follow_up_sms_send`` harness job. Returns a result dict."""
    job = job or {}
    job_id = job.get("id")
    user_id = job.get("user_id")
    correlation_id = job.get("correlation_id")
    payload = _payload_dict(job)
    lead_id = payload.get("lead_id")
    follow_up_id = payload.get("follow_up_id")
    message_id = payload.get("message_id")
    reason_label = payload.get("reason") or "Follow up"
    scheduled_at = payload.get("scheduled_at")

    if not user_id or not lead_id or not message_id:
        logger.error(
            "follow_up_sms_send job missing fields job=%s tenant=%s lead=%s message=%s",
            job_id, user_id, lead_id, message_id,
        )
        return {
            "ok": False, "sent": False, "decision": None,
            "reason": "missing_fields", "approval_id": None, "retryable": False,
        }

    # D5: strict reference validation before any work.
    from harness.validation import ValidationError, validate_lead_reference

    try:
        lead = validate_lead_reference(user_id, lead_id, required=True)
    except ValidationError as exc:
        logger.error(
            "follow_up_sms_send lead validation failed job=%s tenant=%s lead=%s reason=%s",
            job_id, user_id, lead_id, exc.code,
        )
        _audit(
            user_id, action="follow_up_sms_failed", gate_decision=None,
            gate_reason=exc.code, mode=None, target_id=lead_id,
            job_id=job_id, correlation_id=correlation_id,
        )
        return {
            "ok": False, "sent": False, "decision": None,
            "reason": exc.code, "approval_id": None, "retryable": False,
        }

    send_row = _get_send_row_by_job(user_id, job_id)
    if send_row is None:
        logger.error(
            "follow_up_sms_send ledger row missing job=%s tenant=%s", job_id, user_id
        )
        _audit(
            user_id, action="follow_up_sms_failed", gate_decision=None,
            gate_reason="follow_up_send_not_found", mode=None, target_id=lead_id,
            job_id=job_id, correlation_id=correlation_id,
        )
        return {
            "ok": False, "sent": False, "decision": None,
            "reason": "follow_up_send_not_found", "approval_id": None, "retryable": False,
        }

    try:
        sms_row = db.get_sms_message(message_id, user_id)
    except Exception:
        sms_row = None
    if not sms_row:
        logger.error(
            "follow_up_sms_send message row missing job=%s tenant=%s message=%s",
            job_id, user_id, message_id,
        )
        _update_send_row(send_row["id"], status="dead_letter",
                         last_gate_reason="message_not_found")
        _set_ledger_status(user_id, job_id, "dead_letter")
        _audit(
            user_id, action="follow_up_sms_failed", gate_decision=None,
            gate_reason="message_not_found", mode=None, target_id=lead_id,
            job_id=job_id, correlation_id=correlation_id,
        )
        return {
            "ok": False, "sent": False, "decision": None,
            "reason": "message_not_found", "approval_id": None, "retryable": False,
        }

    body = str(sms_row.get("message_body") or "").strip()
    mode = None
    try:
        from harness.sms_handler import get_harness_mode

        mode, paused, _ai_enabled = get_harness_mode(user_id)
    except Exception:
        mode, paused = "drafts_only", False

    # Layer 4 (exactly-once): the send already happened if the outbound
    # row is sent/delivered/queued or carries a provider id — e.g. the
    # job crashed after provider-accept but before complete_job and was
    # reclaimed. Settle without calling the provider again.
    if sms_row.get("status") in _SENT_STATUSES or sms_row.get("provider_message_id"):
        _complete_send_side_effects(user_id, lead_id, follow_up_id, send_row, sms_row)
        logger.info(
            "follow_up_sms already_sent job=%s tenant=%s lead=%s message=%s",
            job_id, user_id, lead_id, message_id,
        )
        return {
            "ok": True, "sent": False, "decision": "ALLOW",
            "reason": "already_sent", "approval_id": None, "retryable": False,
            "message_id": message_id,
            "provider_message_id": sms_row.get("provider_message_id"),
        }

    # Phase III-3: the ledger row for this job. The handler claims it
    # ('scheduled' -> 'claimed') so a concurrent firing for the same
    # triple sees it in flight; the guard below excludes this row.
    from harness.outreach_ledger import (
        check_outreach_allowed,
        get_ledger_row_by_job,
    )

    ledger_row = get_ledger_row_by_job(user_id, job_id)
    if ledger_row is not None and ledger_row.get("status") == "scheduled":
        _set_ledger_status(user_id, job_id, "claimed")
        ledger_row = dict(ledger_row, status="claimed")

    # Lead+reason guard (ruling 3, ledger-backed): in-flight or within
    # 7 days of a sent row for the same (tenant, lead, reason_norm)
    # BLOCKs. The refusal names the prior outreach in the audit row
    # and on the lead timeline (criterion 2).
    reason_norm = crm_db.normalize_follow_up_reason(
        send_row.get("reason") or reason_label
    )
    allowed, duplicate_row, duplicate_scope = check_outreach_allowed(
        user_id,
        lead_id,
        reason_norm,
        exclude_ledger_id=ledger_row["id"] if ledger_row else None,
    )
    if not allowed:
        return _handle_block(
            user_id, lead_id, job_id, send_row, sms_row, mode,
            "duplicate_follow_up_send", correlation_id,
            conflicting_row=duplicate_row,
        )

    # C2 (early): never burn a provider call on a job that cannot send
    # right now. Drafts Only / paused short-circuit to DRAFT later and
    # never send, so they skip this check.
    if mode != "drafts_only" and not paused and _is_rate_limited(
        user_id, (lead or {}).get("phone_number") or ""
    ):
        _audit(
            user_id,
            action="follow_up_sms_deferred",
            gate_decision="DEFER",
            gate_reason="tenant/per-contact rate limit reached; retry later",
            mode=mode,
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
        )
        _needs_attention(user_id, lead_id, message_id, "follow_up_sms_deferred")
        logger.info(
            "follow_up_sms DEFERRED by rate limit before send (will retry) job=%s tenant=%s lead=%s",
            job_id, user_id, lead_id,
        )
        return {
            "ok": False, "sent": False, "decision": "DEFER",
            "reason": "tenant/per-contact rate limit reached; retry later",
            "approval_id": None, "retryable": True,
        }

    # Policy gate (fresh reads at fire time).
    decision, gate_reason, rate_limited, quiet_hours = _decide_follow_up_policy(
        user_id, lead, mode, paused, correlation_id=correlation_id
    )

    if decision == "DRAFT":
        if not body:
            _audit(
                user_id,
                action="follow_up_sms_drafted",
                gate_decision="DRAFT",
                gate_reason="empty_draft",
                mode=mode,
                target_id=lead_id,
                job_id=job_id,
                correlation_id=correlation_id,
            )
            return {
                "ok": True, "sent": False, "decision": "DRAFT",
                "reason": "empty_draft", "approval_id": None, "retryable": False,
            }
        return _handle_draft(
            user_id, lead_id, job_id, send_row, sms_row, body, mode,
            gate_reason, follow_up_id, scheduled_at, correlation_id,
        )

    if decision == "BLOCK":
        return _handle_block(
            user_id, lead_id, job_id, send_row, sms_row, mode,
            gate_reason, correlation_id,
        )

    if decision == "DEFER":
        if rate_limited:
            _audit(
                user_id,
                action="follow_up_sms_deferred",
                gate_decision="DEFER",
                gate_reason=gate_reason,
                mode=mode,
                target_id=lead_id,
                job_id=job_id,
                correlation_id=correlation_id,
            )
            _needs_attention(user_id, lead_id, message_id, "follow_up_sms_deferred")
            logger.info(
                "follow_up_sms DEFERRED by rate limit (will retry) job=%s tenant=%s lead=%s",
                job_id, user_id, lead_id,
            )
            return {
                "ok": False, "sent": False, "decision": "DEFER",
                "reason": gate_reason, "approval_id": None, "retryable": True,
            }
        if quiet_hours:
            return _handle_quiet_hours_defer(
                user_id, lead, job_id, send_row, sms_row, mode,
                gate_reason, correlation_id,
            )
        # Any other DEFER settles completed with no send (no known
        # cause remains after the rate-limit / quiet-hours branches).
        _audit(
            user_id,
            action="follow_up_sms_deferred",
            gate_decision="DEFER",
            gate_reason=gate_reason,
            mode=mode,
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
        )
        _needs_attention(user_id, lead_id, message_id, "follow_up_sms_deferred")
        return {
            "ok": True, "sent": False, "decision": "DEFER",
            "reason": gate_reason, "approval_id": None, "retryable": False,
        }

    # ALLOW -----------------------------------------------------------------
    if not body:
        _update_send_row(send_row["id"], status="dead_letter",
                         last_gate_reason="empty_body")
        _set_ledger_status(user_id, job_id, "dead_letter")
        return {
            "ok": False, "sent": False, "decision": "ALLOW",
            "reason": "empty_body", "approval_id": None, "retryable": False,
        }

    # Hard rule, re-checked immediately before any send path:
    # drafts_only or paused can never reach the provider, whatever the
    # policy said.
    if mode == "drafts_only" or paused:
        return _handle_draft(
            user_id, lead_id, job_id, send_row, sms_row, body, mode,
            "allow_overridden_drafts_only_or_paused", follow_up_id,
            scheduled_at, correlation_id,
        )

    # Atomic spend reservation (defence in depth beyond the gate's
    # point-in-time cap read): a refusal BLOCKs without a provider call.
    from harness.costs import (
        MICROS_PER_CENT,
        get_sms_price_cents_per_unit,
        release_reservation,
        reserve_spend,
    )

    segments = max(1, math.ceil(len(body) / 153))
    estimated_micros = (
        segments
        * get_sms_price_cents_per_unit(config.SMS_PROVIDER)
        * MICROS_PER_CENT
    )
    reservation_corr = f"follow-up-sms:{job_id}"
    if not reserve_spend(user_id, estimated_micros, reservation_corr):
        return _handle_block(
            user_id, lead_id, job_id, send_row, sms_row, mode,
            "spend_cap_reached", correlation_id,
        )

    # Claim the pre-created outbound row BEFORE the provider call
    # (layer 4): only the winner sends; a retry finds the row already
    # sent (above) or still sending/failed and reuses it.
    claim_state, current_row = _claim_outbound_row(message_id)
    if claim_state == "already_sent":
        release_reservation(user_id, reservation_corr)
        _complete_send_side_effects(
            user_id, lead_id, follow_up_id, send_row, current_row
        )
        return {
            "ok": True, "sent": False, "decision": "ALLOW",
            "reason": "already_sent", "approval_id": None, "retryable": False,
            "message_id": message_id,
            "provider_message_id": current_row.get("provider_message_id"),
        }
    if claim_state == "unclaimable":
        release_reservation(user_id, reservation_corr)
        _update_send_row(send_row["id"], status="dead_letter",
                         last_gate_reason="message_row_unclaimable")
        _set_ledger_status(user_id, job_id, "dead_letter")
        return {
            "ok": False, "sent": False, "decision": "ALLOW",
            "reason": "message_row_unclaimable", "approval_id": None,
            "retryable": False,
        }
    # claim_state is "claimed" or "sending" (same-job retry): proceed.

    try:
        from sms_outbound import send_authorized_sms

        result, err, _status = send_authorized_sms(
            user_id,
            lead_id,
            body,
            source_page="follow_up_sms_send",
            compliance_confirmed=True,
            message_id=message_id,
            message_purpose="real_estate_follow_up",
            schedule_if_quiet=False,
            cost_tool="follow_up_sms_send",
        )
    finally:
        release_reservation(user_id, reservation_corr)

    if err:
        err_text = str(err or "")
        # Quiet hours started between gate and send: reschedule rather
        # than drop. The row was claimed; return it to draft so the
        # rescheduled firing claims it cleanly.
        try:
            from sms_quiet_hours import is_quiet_hours_block

            quiet_block = is_quiet_hours_block(err_text)
        except Exception:
            quiet_block = "quiet hours" in err_text.lower()
        if quiet_block:
            _set_sms_row_draft(message_id, error_message=err_text[:500])
            return _handle_quiet_hours_defer(
                user_id, lead, job_id, send_row, sms_row, mode,
                "quiet_hours", correlation_id,
            )
        lowered = err_text.lower()
        if any(marker in lowered for marker in _DETERMINISTIC_SEND_ERROR_MARKERS):
            # Deterministic decline: settle as BLOCK, never retried.
            return _handle_block(
                user_id, lead_id, job_id, send_row, sms_row, mode,
                err_text[:500], correlation_id, reset_sms_row=True,
            )
        # Transient provider error: retry with backoff. The row stays
        # failed/sending (send_authorized_sms marks provider failures
        # failed); the retry reuses the same row via the claim above.
        _audit(
            user_id,
            action="follow_up_sms_send_failed",
            gate_decision="ALLOW",
            gate_reason=err_text[:500],
            mode=mode,
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
        )
        return {
            "ok": False, "sent": False, "decision": "ALLOW",
            "reason": err_text[:500], "approval_id": None, "retryable": True,
        }

    # Provider accepted the message: send_authorized_sms already updated
    # the sms row (status + provider id), recorded the cost row (tool
    # follow_up_sms_send) and touched the lead. Settle the ledger + CRM.
    result = result or {}
    provider_message_id = result.get("provider_message_id")
    try:
        sms_row = db.get_sms_message(message_id, user_id) or sms_row
    except Exception:
        pass
    _complete_send_side_effects(user_id, lead_id, follow_up_id, send_row, sms_row)
    try:
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "follow_up_sms_sent",
            "Follow-up SMS sent",
            {
                "message_id": message_id,
                "provider_message_id": provider_message_id,
                "follow_up_id": follow_up_id,
                "reason": reason_label,
            },
        )
    except Exception:
        logger.exception("follow_up_sms lead-activity write failed lead=%s", lead_id)
    _audit(
        user_id,
        action="follow_up_sms_sent",
        gate_decision="ALLOW",
        gate_reason=gate_reason,
        mode=mode,
        target_id=lead_id,
        job_id=job_id,
        correlation_id=correlation_id,
    )
    logger.info(
        "follow_up_sms sent job=%s tenant=%s lead=%s message=%s provider=%s",
        job_id, user_id, lead_id, message_id, provider_message_id,
    )
    return {
        "ok": True,
        "sent": True,
        "decision": "ALLOW",
        "reason": gate_reason,
        "approval_id": None,
        "retryable": False,
        "message_id": message_id,
        "provider_message_id": provider_message_id,
    }
