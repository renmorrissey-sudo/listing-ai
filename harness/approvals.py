"""Human-approval flow for the AI Harness (M1 + M2).

``harness_approvals`` rows were write-only in Phase 1: created by
``harness.sms_handler`` (DRAFT path) and only COUNTed by
``harness.routes``. This module makes them actionable:

* ``approve_approval`` / ``reject_approval`` — human decisions, taken
  from the API routes (``harness.routes``) or the status page.
* ``execute_approved`` — runs an approved action through the EXISTING
  send path (``sms_ai_agent.process_inbound_ai``), never a new one,
  after re-checking the policy gate against a fresh lead read.
* ``process_execute_approval_job`` — the worker entry point for the
  ``execute_approval`` job kind (see ``workers/harness_worker.py``).
  Approving enqueues exactly one such job (idempotency key
  ``execute-approval-<approval_id>``); rejecting enqueues nothing.

Approval kinds: ``"sms_reply"`` (Phase 1, ``draft_json`` carries
``draft_reply`` + ``lead_id`` + ``inbound_id``, as created by
``harness.sms_handler``) and ``"follow_up_sms"`` (Phase III-1,
``draft_json`` carries ``draft_reply`` + ``lead_id`` + ``message_id``
— no inbound id; execution sends via ``sms_outbound.
send_authorized_sms`` with the stored message id, never
``process_inbound_ai``).

Expiry: a pending approval expires 7 days after ``created_at``
(``EXPIRY_DAYS``). Migration 029 has no ``expires_at`` column, so expiry
is derived from ``created_at`` at read time — no migration in this step.
An expired approval cannot be approved or rejected; the attempt marks it
``expired`` and audits it.

Statuses: ``pending`` -> ``approved`` -> ``executed`` (or ``blocked``);
``pending`` -> ``rejected``; ``pending`` -> ``expired``. ``executing`` is
a transient claim state held only while the send is in flight, so a
second (concurrent or repeated) execution cannot send twice; a failed
send reverts the row to ``approved`` so the worker retry can proceed.
A worker killed mid-send leaves the row ``executing`` — visible in the
audit trail (approved, no execution) — rather than risking a double send.

Every transition writes one ``harness_audit`` row (actor ``"user"`` for
approve/reject, ``"system"`` for execute/block/expire). Every query is
user_id-scoped: a tenant touching another tenant's approval sees it as
not found (the routes map that to 404 and never reveal it exists).

Out of scope (deliberate): the Ask TopAI confirmation-token path
was removed in Phase I hardening (2026-11-08) — it could never
succeed (no token was ever issued or stored, so ``confirm()`` could
only return 400) and nothing called it. This harness approvals path
is the approval flow that actually executes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

EXPIRY_DAYS = 7

# Terminal statuses — execute_approved never sends for these.
TERMINAL_STATUSES = frozenset(
    {"rejected", "executed", "blocked", "expired"}
)

# Send-path decline reasons that are deterministic: retrying the same
# approved send will produce the same decline, so the approval is
# settled as blocked (never sent, never retried) instead of reverting
# to approved and retrying forever. Everything NOT in this set is
# treated as transient (provider error, coach failure) and reverts to
# approved for a worker retry. Mirrors the coach's own
# spend_cap_reached -> retryable=False in harness.sms_handler.
DETERMINISTIC_DECLINE_REASONS = frozenset(
    {
        "escalation_topic",  # hard escalation topic (legal/fair housing/complaint)
        "opted_out",
        "consent_blocked",
        "suppressed",
        "inquiry_blocked",
        "auto_reply_disabled",
        "lead_missing",
        "empty_draft",
        "toll_free_not_verified",
        "trial_destination_blocked",
        "spend_cap_reached",
        "reply_rate_limited",
        "no_analysis",
    }
)

# Statuses a human decision may be taken from (pending + not expired).
DECIDABLE_STATUSES = frozenset({"pending"})


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _set_ledger_status_by_approval(user_id, approval_id, status, **fields):
    """Mirror a lifecycle transition onto the outreach ledger row for
    this approval (Phase III-3). No-op with a warning when the row is
    missing (a pre-047 approval the backfill did not cover) — the
    approval itself must never fail for a missing ledger row."""
    try:
        from harness.outreach_ledger import (
            get_ledger_row_by_approval,
            update_outreach_status,
        )

        row = get_ledger_row_by_approval(user_id, approval_id)
        if row is None:
            logger.warning(
                "outreach ledger row missing approval=%s tenant=%s status=%s",
                approval_id,
                user_id,
                status,
            )
            return
        update_outreach_status(row["id"], status=status, **fields)
    except Exception:
        logger.warning(
            "outreach ledger status update failed approval=%s tenant=%s status=%s",
            approval_id,
            user_id,
            status,
            exc_info=True,
        )


def _parse_created_at(value):
    """Parse a stored ISO created_at; None when unparseable/missing."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def expires_at_for(created_at):
    """Expiry instant for a pending approval, derived from created_at."""
    created = _parse_created_at(created_at)
    if created is None:
        return None
    return created + timedelta(days=EXPIRY_DAYS)


def is_expired_row(row):
    """True when a pending approval row is past its 7-day expiry."""
    if not row or row.get("status") != "pending":
        return False
    expires_at = expires_at_for(row.get("created_at"))
    if expires_at is None:
        return False
    return _now() >= expires_at


def _row(row):
    return dict(row) if row is not None else None


def _draft_dict(row):
    raw = (row or {}).get("draft_json") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def serialize_approval(row):
    """Public dict for one approval row, with parsed draft + expiry.

    Surfaces the escalation topics and gate reason recorded when the
    draft was created, so the human sees BEFORE approving that the
    inbound was flagged (and why it was drafted rather than sent).
    """
    if not row:
        return None
    draft = _draft_dict(row)
    expires_at = expires_at_for(row.get("created_at"))
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "job_id": row.get("job_id"),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "draft": draft,
        "lead_id": draft.get("lead_id"),
        "inbound_id": draft.get("inbound_id"),
        "draft_reply": draft.get("draft_reply"),
        "escalation_topics": list(draft.get("escalation_topics") or []),
        "gate_reason": draft.get("gate_reason"),
        "created_at": row.get("created_at"),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "decided_by": row.get("decided_by"),
        "decided_at": row.get("decided_at"),
    }


def _audit(user_id, *, action, approval_id, mode=None, gate_decision=None,
           gate_reason=None, actor="system", correlation_id=None):
    """Append one harness_audit row for an approval transition.

    Best-effort: a missing audit table is logged, never raised — the
    status transition itself must not fail because auditing did.
    """
    try:
        from harness.audit import record_audit

        record_audit(
            user_id,
            actor,
            action,
            tool="harness_approvals",
            target_type="approval",
            target_id=approval_id,
            mode=mode,
            gate_decision=gate_decision,
            gate_reason=(gate_reason or "")[:500],
            cost_cents=0,
            request_id=str(approval_id),
            correlation_id=correlation_id or str(approval_id),
        )
    except Exception:
        logger.warning(
            "approval audit write failed tenant=%s approval=%s action=%s",
            user_id,
            approval_id,
            action,
        )


def _mode(user_id):
    try:
        from harness.policy import get_settings

        return (get_settings(user_id) or {}).get("mode")
    except Exception:
        return None


def get_approval(user_id, approval_id):
    """One approval row, user_id-scoped; None when missing/not owned."""
    with db.get_db() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM harness_approvals WHERE id = ? AND user_id = ?",
                (approval_id, user_id),
            ).fetchone()
        )


def _set_status(user_id, approval_id, status, *, decided_by=None):
    """Guarded status update; returns True when exactly one row moved."""
    now = _now_iso()
    with db.get_db() as conn:
        if decided_by is not None:
            cur = conn.execute(
                """
                UPDATE harness_approvals
                SET status = ?, decided_by = ?, decided_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (status, decided_by, now, approval_id, user_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE harness_approvals
                SET status = ?
                WHERE id = ? AND user_id = ?
                """,
                (status, approval_id, user_id),
            )
        return cur.rowcount == 1


def _expire_if_stale(user_id, row):
    """Mark a stale pending row expired (+ audit) and return True.

    Returns False when the row is not expired. Callers re-read the row
    after a True return.
    """
    if not is_expired_row(row):
        return False
    _set_status(user_id, row["id"], "expired")
    # Phase III-4: an expired appointment_request approval expires its
    # request too — the request is no longer confirmable.
    if row.get("kind") == "appointment_request":
        try:
            from harness.appointment_requests import expire_request

            draft = _draft_dict(row)
            if draft.get("request_id"):
                expire_request(user_id, draft["request_id"])
        except Exception:
            logger.exception(
                "appointment request expire failed tenant=%s approval=%s",
                user_id,
                row["id"],
            )
    _audit(
        user_id,
        action="approval_expired",
        approval_id=row["id"],
        mode=_mode(user_id),
        gate_decision="EXPIRED",
        gate_reason="pending approval expired after 7 days without a decision",
        actor="system",
    )
    logger.info(
        "approval expired tenant=%s approval=%s", user_id, row["id"]
    )
    return True


def list_pending_approvals(user_id, limit=50):
    """A tenant's pending approvals, newest first.

    Lazily expires stale pending rows (they leave the pending list and
    are audited as expired). Each item is a serialized approval dict.
    """
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM harness_approvals
            WHERE user_id = ? AND status = 'pending'
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    pending = []
    for raw in rows:
        row = _row(raw)
        if _expire_if_stale(user_id, row):
            continue
        pending.append(serialize_approval(row))
    return pending


def approve_approval(user_id, approval_id):
    """Mark a pending approval approved.

    Returns ``(approval_dict_or_None, error)`` where error is None on
    success, else one of ``"not_found"`` / ``"expired"`` /
    ``"not_pending"``. Approving enqueues nothing itself — the route
    enqueues the ``execute_approval`` job on success.
    """
    row = get_approval(user_id, approval_id)
    if row is None:
        return None, "not_found"
    if _expire_if_stale(user_id, row):
        return serialize_approval(get_approval(user_id, approval_id)), "expired"
    if row.get("status") not in DECIDABLE_STATUSES:
        return serialize_approval(row), "not_pending"
    # D5: validate the draft's write arguments at approve time — a draft
    # whose lead was deleted, moved to another tenant, or whose
    # suggested status is invalid cannot be approved. The approval stays
    # pending and the failure is audited; nothing is enqueued.
    draft = _draft_dict(row)
    if row.get("kind") not in ("sms_reply", "follow_up_sms", "appointment_request"):
        _audit(
            user_id,
            action="approval_validation_failed",
            approval_id=approval_id,
            mode=_mode(user_id),
            gate_decision="BLOCK",
            gate_reason=f"unsupported approval kind: {row.get('kind')}",
            actor="user",
        )
        return serialize_approval(row), "invalid_draft"
    if row.get("kind") == "follow_up_sms":
        from harness.validation import validate_follow_up_sms_draft

        _, draft_error = validate_follow_up_sms_draft(user_id, draft)
    elif row.get("kind") == "appointment_request":
        from harness.validation import validate_appointment_request_draft

        _, draft_error = validate_appointment_request_draft(user_id, draft)
    else:
        from harness.validation import validate_sms_reply_draft

        _, draft_error = validate_sms_reply_draft(user_id, draft)
    if draft_error is not None:
        _audit(
            user_id,
            action="approval_validation_failed",
            approval_id=approval_id,
            mode=_mode(user_id),
            gate_decision="BLOCK",
            gate_reason=f"draft validation failed: {draft_error}",
            actor="user",
        )
        logger.info(
            "approval validation failed tenant=%s approval=%s reason=%s",
            user_id,
            approval_id,
            draft_error,
        )
        return serialize_approval(row), "invalid_draft"
    _set_status(user_id, approval_id, "approved", decided_by=user_id)
    _audit(
        user_id,
        action="approval_approved",
        approval_id=approval_id,
        mode=_mode(user_id),
        gate_decision="APPROVED",
        gate_reason=f"kind={row.get('kind')}",
        actor="user",
    )
    logger.info("approval approved tenant=%s approval=%s", user_id, approval_id)
    return serialize_approval(get_approval(user_id, approval_id)), None


def reject_approval(user_id, approval_id, reason=None):
    """Mark a pending approval rejected with an optional reason.

    Returns ``(approval_dict_or_None, error)`` with the same error
    vocabulary as :func:`approve_approval`. Rejection enqueues nothing.
    """
    row = get_approval(user_id, approval_id)
    if row is None:
        return None, "not_found"
    if _expire_if_stale(user_id, row):
        return serialize_approval(get_approval(user_id, approval_id)), "expired"
    if row.get("status") not in DECIDABLE_STATUSES:
        return serialize_approval(row), "not_pending"
    _set_status(user_id, approval_id, "rejected", decided_by=user_id)
    # Phase III-4: rejecting an appointment_request approval declines
    # the request — the lead is not texted automatically.
    if row.get("kind") == "appointment_request":
        try:
            from harness.appointment_requests import decline_request

            draft = _draft_dict(row)
            if draft.get("request_id"):
                decline_request(user_id, draft["request_id"], reason=reason)
        except Exception:
            logger.exception(
                "appointment request decline failed tenant=%s approval=%s",
                user_id,
                approval_id,
            )
    _audit(
        user_id,
        action="approval_rejected",
        approval_id=approval_id,
        mode=_mode(user_id),
        gate_decision="REJECTED",
        gate_reason=(reason or f"kind={row.get('kind')}"),
        actor="user",
    )
    logger.info(
        "approval rejected tenant=%s approval=%s reason=%s",
        user_id,
        approval_id,
        reason,
    )
    return serialize_approval(get_approval(user_id, approval_id)), None


def enqueue_execute_approval_job(user_id, approval_id):
    """Enqueue the durable ``execute_approval`` job for an approval.

    Idempotency key ``execute-approval-<id>``: approving twice can never
    create a second execution job.
    """
    from harness.jobs import enqueue_job, new_correlation_id

    # L4: one correlation id for approve -> execute job -> send -> audit.
    return enqueue_job(
        user_id,
        "execute_approval",
        {"approval_id": approval_id},
        idempotency_key=f"execute-approval-{approval_id}",
        correlation_id=new_correlation_id(),
    )


# --- Execution ---------------------------------------------------------------


def _load_inbound_body(user_id, inbound_id):
    """Raw inbound body from its sms_messages row (source of truth)."""
    if not inbound_id:
        return ""
    try:
        row = db.get_sms_message(inbound_id, user_id)
    except Exception:
        return ""
    return str((row or {}).get("message_body") or "")


def _is_rate_limited(user_id, phone):
    """Real tenant/contact rate check; fail closed on any check error."""
    try:
        from sms_authorization import _rate_limited

        return bool(_rate_limited(user_id, phone))
    except Exception:
        logger.exception(
            "approval execution rate-limit check failed tenant=%s; failing closed",
            user_id,
        )
        return True


def _template_approved_for_draft(user_id, cleaned_draft):
    """Re-resolve a stored draft's template_id at execution time.

    Returns True only when the id still resolves to an approved SMS
    template for this tenant (an archived template -> False). The
    draft's exact stored text still executes either way — the flag is
    evidence for the gate, and a human approved the text itself.
    """
    template_id = (cleaned_draft or {}).get("template_id")
    if template_id is None:
        return False
    try:
        from harness.templates_db import resolve_template_for_compose

        return resolve_template_for_compose(
            user_id, template_id, channel="sms"
        ) is not None
    except Exception:
        return False


def _execution_gate_context(user_id, lead, template_approved=False):
    """Fresh policy-gate context for an approved send.

    Mirrors ``harness.sms_handler._decide_policy``: consent, opt-out,
    suppression, quiet hours and the real rate-limit check, all read
    fresh at execution time. ``mode`` is passed as ``"authorized"`` to
    ``decide`` because the human approval itself satisfies the mode
    gate — the mode rule is the one check a human decision overrides;
    consent / suppression / quiet-hours / rate-limit / escalation /
    pause still apply in full (``paused`` is read from the tenant's
    real settings, not overridden).
    """
    phone = (lead or {}).get("phone_number") or ""
    paused = False
    try:
        from harness.policy import get_settings

        paused = bool((get_settings(user_id) or {}).get("paused"))
    except Exception:
        paused = False
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
    return {
        "mode": "authorized",
        # Human-approval marker: the approval itself satisfies the mode
        # gate. The staging mode lock in harness.policy honours this
        # marker and nothing else; consent / suppression / quiet-hours /
        # rate-limit / pause still apply in full.
        "human_approval": True,
        "paused": paused,
        "consent_status": (lead or {}).get("sms_consent_status") or "",
        "opted_out": (lead or {}).get("opt_out_status") == "opted_out",
        "suppressed": suppressed,
        "quiet_hours": quiet_hours,
        "rate_limited": _is_rate_limited(user_id, phone),
        "escalation_topics": [],
        # Phase III-5: True only when the stored draft's template_id
        # still resolves to an approved template for this tenant.
        # The mode is passed as "authorized" here, so the flag is a
        # no-op for the decision — it is carried for truthfulness and
        # evidence.
        "template_approved": bool(template_approved),
    }


def _mark_blocked(user_id, approval_id, decision, reason):
    _set_status(user_id, approval_id, "blocked")
    _audit(
        user_id,
        action="approval_blocked",
        approval_id=approval_id,
        mode=_mode(user_id),
        gate_decision=decision,
        gate_reason=reason,
        actor="system",
    )
    logger.info(
        "approval blocked at execution tenant=%s approval=%s reason=%s",
        user_id,
        approval_id,
        reason,
    )


def _follow_up_execution_gate_context(user_id, lead, template_approved=False):
    """Fresh policy-gate context for an approved follow-up send.

    The shared :func:`_execution_gate_context` plus the two fields the
    follow-up fire-time gate must re-check: ``do_not_contact`` (a lead
    marked do-not-contact after the draft was created must block) and
    ``consent_missing`` (consent evidence revoked after scheduling must
    block). Built separately from the shared context so the sms_reply
    execution path keeps its established behaviour.
    """
    context = _execution_gate_context(
        user_id, lead, template_approved=template_approved
    )
    context["do_not_contact"] = (lead or {}).get("status") == "do_not_contact"
    try:
        from harness.followup_sends import has_messaging_consent_evidence

        context["consent_missing"] = not has_messaging_consent_evidence(
            user_id, lead
        )
    except Exception:
        context["consent_missing"] = False
    return context


def _execute_follow_up_sms_approval(user_id, approval_id, row, draft, correlation_id=None):
    """Execute an approved ``follow_up_sms`` approval.

    Called with the row already claimed (``approved`` -> ``executing``).
    Sends the EXACT stored draft via ``sms_outbound.send_authorized_sms``
    with the stored ``message_id`` — never ``process_inbound_ai`` (a
    follow-up has no inbound message). The policy gate is re-checked
    with a fresh lead read before any send; a non-ALLOW verdict marks
    the approval ``blocked``. If the stored message row is already
    sent, the approval is recorded executed without sending again.
    """
    from harness.validation import validate_follow_up_sms_draft

    cleaned_draft, draft_error = validate_follow_up_sms_draft(user_id, draft)
    if draft_error is not None:
        reason = f"validation_failed: {draft_error}"
        _mark_blocked(user_id, approval_id, "BLOCK", reason)
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": reason,
            "approval_id": approval_id,
            "retryable": False,
        }
    lead_id = cleaned_draft["lead_id"]
    message_id = cleaned_draft.get("message_id")
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        _mark_blocked(user_id, approval_id, "BLOCK", "lead_missing")
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": "lead_missing",
            "approval_id": approval_id,
            "retryable": False,
        }
    if not message_id:
        _mark_blocked(user_id, approval_id, "BLOCK", "message_not_found")
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": "message_not_found",
            "approval_id": approval_id,
            "retryable": False,
        }

    # Already sent (another path sent the stored row): exactly-once
    # holds; record executed, do not send again.
    try:
        sms_row = db.get_sms_message(message_id, user_id)
    except Exception:
        sms_row = None
    if sms_row and (
        sms_row.get("status") in {"sent", "delivered", "queued", "mock_sent"}
        or sms_row.get("provider_message_id")
    ):
        _set_status(user_id, approval_id, "executed")
        # The send did happen (via another path): the ledger row for
        # this approval is a sent row, so the guard sees it.
        _set_ledger_status_by_approval(
            user_id, approval_id, "sent",
            sent_at=sms_row.get("sent_at") or _now_iso(),
        )
        _audit(
            user_id,
            action="approval_executed",
            approval_id=approval_id,
            correlation_id=correlation_id,
            mode=_mode(user_id),
            gate_decision="ALLOW",
            gate_reason="already_sent",
            actor="system",
        )
        return {
            "ok": True,
            "sent": False,
            "decision": "ALLOW",
            "reason": "already_sent",
            "approval_id": approval_id,
            "retryable": False,
            "message_id": message_id,
        }

    # Execution-time policy re-check with a fresh lead read.
    try:
        from harness.policy import decide

        decision, gate_reason = decide(
            user_id, "send_sms", "send_sms",
            _follow_up_execution_gate_context(
                user_id, lead,
                template_approved=_template_approved_for_draft(
                    user_id, cleaned_draft
                ),
            ),
        )
    except Exception:
        logger.exception(
            "follow-up approval execution policy check failed tenant=%s approval=%s",
            user_id,
            approval_id,
        )
        _mark_blocked(user_id, approval_id, "BLOCK", "policy_error")
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": "policy_error",
            "approval_id": approval_id,
            "retryable": False,
        }
    decision = str(decision or "BLOCK").upper()
    if decision != "ALLOW":
        reason = gate_reason or f"policy_{decision.lower()}"
        _mark_blocked(user_id, approval_id, decision, reason)
        return {
            "ok": True,
            "sent": False,
            "decision": decision,
            "reason": reason,
            "approval_id": approval_id,
            "retryable": False,
        }

    body = str(cleaned_draft.get("draft_reply") or "").strip()
    if not body:
        _mark_blocked(user_id, approval_id, "BLOCK", "empty_draft")
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": "empty_draft",
            "approval_id": approval_id,
            "retryable": False,
        }
    # Execute-time duplicate limb (Phase III-3, criterion 1a): a
    # human-approved send can still be refused at execution when the
    # twin approval already sent for the same (tenant, lead, reason)
    # triple within 7 days. Only the sent-within-window limb applies
    # here — the fire-time check already covered the in-flight limb,
    # and a 'drafted' row belonging to a live approval is the twin
    # this limb must NOT block on (this approval IS that twin).
    # First executor wins; the loser is refused with the prior send
    # named in the audit row and on the lead timeline.
    from harness.outreach_ledger import (
        check_outreach_allowed,
        format_prior_outreach,
        get_ledger_row_by_approval,
        write_duplicate_timeline_entry,
    )

    _send_row = None
    _follow_up_send_id = cleaned_draft.get("follow_up_send_id")
    if _follow_up_send_id is not None:
        try:
            with db.get_db() as _conn:
                _send_row = _conn.execute(
                    "SELECT * FROM harness_follow_up_sends "
                    "WHERE id = ? AND user_id = ?",
                    (_follow_up_send_id, user_id),
                ).fetchone()
        except Exception:
            _send_row = None
    _reason_norm = None
    if _send_row is not None:
        _reason_norm = _send_row.get("reason_norm")
        if not _reason_norm and _send_row.get("reason"):
            from crm_db import normalize_follow_up_reason

            _reason_norm = normalize_follow_up_reason(_send_row["reason"])
    if _reason_norm:
        _ledger_row = get_ledger_row_by_approval(user_id, approval_id)
        _allowed, _conflicting, _scope = check_outreach_allowed(
            user_id,
            lead_id,
            _reason_norm,
            execute_time=True,
            exclude_ledger_id=_ledger_row["id"] if _ledger_row else None,
        )
        if not _allowed:
            _reason = (
                f"duplicate_follow_up_send: "
                f"{format_prior_outreach(_conflicting)}"
            )[:500]
            _mark_blocked(user_id, approval_id, "BLOCK", _reason)
            write_duplicate_timeline_entry(user_id, lead_id, _conflicting)
            _set_ledger_status_by_approval(user_id, approval_id, "blocked")
            return {
                "ok": True,
                "sent": False,
                "decision": "BLOCK",
                "reason": _reason,
                "approval_id": approval_id,
                "retryable": False,
            }
    try:
        from sms_outbound import send_authorized_sms

        result, err, _status = send_authorized_sms(
            user_id,
            lead_id,
            body,
            source_page="follow_up_sms_approval",
            compliance_confirmed=True,
            message_id=message_id,
            message_purpose="real_estate_follow_up",
            schedule_if_quiet=False,
            cost_tool="follow_up_sms_send",
        )
    except Exception as exc:
        logger.exception(
            "follow-up approval execution send failed tenant=%s approval=%s",
            user_id,
            approval_id,
        )
        _set_status(user_id, approval_id, "approved")
        return {
            "ok": False,
            "sent": False,
            "decision": "ALLOW",
            "reason": f"send_error: {exc}"[:500],
            "approval_id": approval_id,
            "retryable": True,
        }
    if err:
        reason = str(err)[:500]
        if reason in DETERMINISTIC_DECLINE_REASONS or any(
            marker in reason.lower()
            for marker in (
                "opted out", "suppression", "consent", "certif",
                "not permitted", "revoked", "trial destination",
                "toll-free", "not verified", "quiet hours",
            )
        ):
            _mark_blocked(user_id, approval_id, "BLOCK", reason)
            return {
                "ok": True,
                "sent": False,
                "decision": "BLOCK",
                "reason": reason,
                "approval_id": approval_id,
                "retryable": False,
            }
        _set_status(user_id, approval_id, "approved")
        return {
            "ok": False,
            "sent": False,
            "decision": "ALLOW",
            "reason": reason,
            "approval_id": approval_id,
            "retryable": True,
        }
    # The ledger row goes to 'sent' BEFORE the approval is marked
    # executed, so a crash between the two still leaves the guard
    # correct (Phase III-3).
    _set_ledger_status_by_approval(
        user_id, approval_id, "sent", sent_at=_now_iso(),
    )
    _set_status(user_id, approval_id, "executed")
    _audit(
        user_id,
        action="approval_executed",
        approval_id=approval_id,
        correlation_id=correlation_id,
        mode=_mode(user_id),
        gate_decision="ALLOW",
        gate_reason=(result or {}).get("reason") or gate_reason or "sent",
        actor="system",
    )
    logger.info(
        "follow-up approval executed tenant=%s approval=%s lead=%s message=%s",
        user_id,
        approval_id,
        lead_id,
        (result or {}).get("provider_message_id"),
    )
    return {
        "ok": True,
        "sent": True,
        "decision": "ALLOW",
        "reason": (result or {}).get("reason") or "sent",
        "approval_id": approval_id,
        "retryable": False,
        "message_id": message_id,
        "provider_message_id": (result or {}).get("provider_message_id"),
    }


def _execute_appointment_request_approval(user_id, approval_id, row, draft, correlation_id=None):
    """Execute an approved ``appointment_request`` approval.

    Called with the row already claimed (``approved`` -> ``executing``).
    Delegates to ``harness.appointment_requests.confirm_request`` and
    maps the outcome onto the approval status: confirmed -> executed
    (+ ``approval_executed`` audit); conflicted / no_calendar /
    policy-blocked / no-slot -> blocked with the reason; a transient
    failure (calendar unreadable) -> approval back to "approved",
    retryable=True so the worker retries with backoff.
    """
    from harness.appointment_requests import confirm_request
    from harness.validation import validate_appointment_request_draft

    cleaned_draft, draft_error = validate_appointment_request_draft(user_id, draft)
    if draft_error is not None:
        reason = f"validation_failed: {draft_error}"
        _mark_blocked(user_id, approval_id, "BLOCK", reason)
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": reason,
            "approval_id": approval_id,
            "retryable": False,
        }
    try:
        result = confirm_request(
            user_id,
            cleaned_draft["request_id"],
            correlation_id=correlation_id,
        )
    except Exception as exc:
        logger.exception(
            "appointment request confirm failed tenant=%s approval=%s",
            user_id,
            approval_id,
        )
        _set_status(user_id, approval_id, "approved")
        return {
            "ok": False,
            "sent": False,
            "decision": "ALLOW",
            "reason": f"confirm_error: {exc}"[:500],
            "approval_id": approval_id,
            "retryable": True,
        }
    if result.get("ok"):
        _set_status(user_id, approval_id, "executed")
        _audit(
            user_id,
            action="approval_executed",
            approval_id=approval_id,
            correlation_id=correlation_id,
            mode=_mode(user_id),
            gate_decision="ALLOW",
            gate_reason=result.get("reason") or "confirmed",
            actor="system",
        )
        logger.info(
            "appointment request approval executed tenant=%s approval=%s request=%s appointment=%s",
            user_id,
            approval_id,
            cleaned_draft["request_id"],
            result.get("appointment_id"),
        )
        return {
            "ok": True,
            "sent": True,
            "decision": "ALLOW",
            "reason": result.get("reason") or "confirmed",
            "approval_id": approval_id,
            "retryable": False,
            "appointment_id": result.get("appointment_id"),
            "external_event_id": result.get("external_event_id"),
            "message_id": result.get("message_id"),
        }
    if result.get("retryable"):
        _set_status(user_id, approval_id, "approved")
        return {
            "ok": False,
            "sent": False,
            "decision": "DEFER",
            "reason": str(result.get("reason") or "retryable")[:500],
            "approval_id": approval_id,
            "retryable": True,
        }
    reason = str(result.get("reason") or "confirm_refused")[:500]
    _mark_blocked(user_id, approval_id, "BLOCK", reason)
    return {
        "ok": True,
        "sent": False,
        "decision": "BLOCK",
        "reason": reason,
        "approval_id": approval_id,
        "retryable": False,
    }


def execute_approved(user_id, approval_id, correlation_id=None):
    """Execute an approved approval. Returns a worker result dict.

    Result keys mirror ``harness.sms_handler``: ``ok``, ``sent``,
    ``decision``, ``reason``, ``approval_id``, ``retryable``.
    ``ok=True`` means the job is settled (sent, blocked, or already
    handled — never retried); ``ok=False, retryable=True`` means a
    transient send failure worth retrying with backoff.

    Only ``status='approved'`` executes, and the claim
    (``approved`` -> ``executing``) is a guarded UPDATE, so a second
    execution — concurrent or repeated — finds no approved row and
    sends nothing. The policy gate is re-checked with a fresh lead read
    before any send; a non-ALLOW verdict marks the approval ``blocked``
    (never sent) and audits the reason.
    """
    row = get_approval(user_id, approval_id)
    if row is None:
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": "not_found",
            "approval_id": approval_id,
            "retryable": False,
        }
    status = row.get("status")
    if status == "executed":
        return {
            "ok": True,
            "sent": False,
            "decision": "ALREADY_EXECUTED",
            "reason": "approval already executed; not sending again",
            "approval_id": approval_id,
            "retryable": False,
        }
    if status in TERMINAL_STATUSES:
        return {
            "ok": True,
            "sent": False,
            "decision": "NOT_APPROVED",
            "reason": f"approval is {status}; not sending",
            "approval_id": approval_id,
            "retryable": False,
        }
    if status == "executing":
        # A previous attempt claimed it (or crashed mid-send). Never
        # send twice for the same approval; the claim holder owns it.
        return {
            "ok": True,
            "sent": False,
            "decision": "ALREADY_EXECUTING",
            "reason": "approval execution already in progress; not sending again",
            "approval_id": approval_id,
            "retryable": False,
        }
    if status != "approved":
        return {
            "ok": True,
            "sent": False,
            "decision": "NOT_APPROVED",
            "reason": f"approval is {status}; not sending",
            "approval_id": approval_id,
            "retryable": False,
        }

    # Guarded claim: only the caller that moves approved -> executing
    # owns this send. A lost claim race returns no row to send for.
    with db.get_db() as conn:
        cur = conn.execute(
            """
            UPDATE harness_approvals
            SET status = 'executing'
            WHERE id = ? AND user_id = ? AND status = 'approved'
            """,
            (approval_id, user_id),
        )
        if cur.rowcount != 1:
            return {
                "ok": True,
                "sent": False,
                "decision": "NOT_APPROVED",
                "reason": "approval was already claimed or decided; not sending",
                "approval_id": approval_id,
                "retryable": False,
            }

    draft = _draft_dict(row)
    kind = row.get("kind")
    if kind == "follow_up_sms":
        return _execute_follow_up_sms_approval(
            user_id, approval_id, row, draft, correlation_id=correlation_id
        )
    if kind == "appointment_request":
        return _execute_appointment_request_approval(
            user_id, approval_id, row, draft, correlation_id=correlation_id
        )
    if kind != "sms_reply":
        _mark_blocked(user_id, approval_id, "BLOCK", f"unsupported approval kind: {kind}")
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": f"unsupported approval kind: {kind}",
            "approval_id": approval_id,
            "retryable": False,
        }
    # D5: revalidate the draft's write arguments at execution time, with
    # a fresh lead read. A job enqueued with valid args that later became
    # invalid (lead deleted, moved to another tenant, inbound dropped,
    # invalid suggested status) fails safely here instead of executing.
    from harness.validation import validate_sms_reply_draft

    cleaned_draft, draft_error = validate_sms_reply_draft(user_id, draft)
    if draft_error is not None:
        reason = f"validation_failed: {draft_error}"
        _mark_blocked(user_id, approval_id, "BLOCK", reason)
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": reason,
            "approval_id": approval_id,
            "retryable": False,
        }
    lead_id = cleaned_draft["lead_id"]
    inbound_id = cleaned_draft["inbound_id"]
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        # Deleted between validation and this read: fail safely.
        _mark_blocked(user_id, approval_id, "BLOCK", "lead_missing")
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": "lead_missing",
            "approval_id": approval_id,
            "retryable": False,
        }

    # Execution-time policy re-check with a fresh lead read.
    try:
        from harness.policy import decide

        decision, gate_reason = decide(
            user_id, "send_sms", "send_sms",
            _execution_gate_context(
                user_id, lead,
                template_approved=_template_approved_for_draft(
                    user_id, cleaned_draft
                ),
            ),
        )
    except Exception:
        logger.exception(
            "approval execution policy check failed tenant=%s approval=%s",
            user_id,
            approval_id,
        )
        _mark_blocked(user_id, approval_id, "BLOCK", "policy_error")
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": "policy_error",
            "approval_id": approval_id,
            "retryable": False,
        }
    decision = str(decision or "BLOCK").upper()
    if decision != "ALLOW":
        reason = gate_reason or f"policy_{decision.lower()}"
        _mark_blocked(user_id, approval_id, decision, reason)
        return {
            "ok": True,
            "sent": False,
            "decision": decision,
            "reason": reason,
            "approval_id": approval_id,
            "retryable": False,
        }

    # Approved + gate ALLOW: send the EXACT stored human-approved
    # draft through the existing send path, marked as a human-approved
    # send. The stored draft (cleaned_draft) is passed through so the
    # coach is NOT re-run — a fresh model call could generate a
    # different draft, and the content executed must match the stored
    # human-approved message (approved-draft fidelity). The approval
    # overrides the routine-verification escalation topics (uncertain
    # property facts, negotiation, financing) — a human has reviewed
    # the draft that answers them. Hard escalation topics (legal, fair
    # housing, complaint) still block inside the send path, and every
    # other control (consent, opt-out, suppression, rate limit, quiet
    # hours, spend cap) applies in full. If any check requires the
    # text to change, the send is blocked (never silently modified).
    body = _load_inbound_body(user_id, inbound_id) or str(draft.get("body") or "")
    receiving_number = draft.get("receiving_number")
    try:
        from sms_ai_agent import process_inbound_ai

        send_result = process_inbound_ai(
            user_id, lead_id, inbound_id, body, receiving_number,
            approved=True, approved_draft=cleaned_draft,
        ) or {}
    except Exception as exc:
        logger.exception(
            "approval execution send failed tenant=%s approval=%s", user_id, approval_id
        )
        # Transient failure: revert to approved so the worker retry can
        # claim it again. The provider send is inside process_inbound_ai,
        # whose reply-slot claim dedupes a send that actually went out.
        _set_status(user_id, approval_id, "approved")
        return {
            "ok": False,
            "sent": False,
            "decision": "ALLOW",
            "reason": f"send_error: {exc}"[:500],
            "approval_id": approval_id,
            "retryable": True,
        }

    if send_result.get("replied"):
        _set_status(user_id, approval_id, "executed")
        _audit(
            user_id,
            action="approval_executed",
            approval_id=approval_id,
                correlation_id=correlation_id,
            mode=_mode(user_id),
            gate_decision="ALLOW",
            gate_reason=send_result.get("reason") or gate_reason or "sent",
            actor="system",
        )
        logger.info(
            "approval executed tenant=%s approval=%s lead=%s message=%s",
            user_id,
            approval_id,
            lead_id,
            send_result.get("message_id"),
        )
        return {
            "ok": True,
            "sent": True,
            "decision": "ALLOW",
            "reason": send_result.get("reason") or "sent",
            "approval_id": approval_id,
            "retryable": False,
            "message_id": send_result.get("message_id"),
        }
    if send_result.get("reason") == "already_replied":
        # The reply already went out (another path claimed the slot):
        # exactly-once holds; record it executed, do not send again.
        _set_status(user_id, approval_id, "executed")
        _audit(
            user_id,
            action="approval_executed",
            approval_id=approval_id,
                correlation_id=correlation_id,
            mode=_mode(user_id),
            gate_decision="ALLOW",
            gate_reason="already_replied",
            actor="system",
        )
        return {
            "ok": True,
            "sent": False,
            "decision": "ALLOW",
            "reason": "already_replied",
            "approval_id": approval_id,
            "retryable": False,
        }
    # Send path declined without sending. A deterministic decline
    # (policy/compliance/consent — the same send would be declined
    # again) settles the approval as blocked: never sent, never
    # retried, with the reason audited so the human can see why. Only
    # a transient failure (provider error, coach failure) reverts to
    # approved for a worker retry.
    reason = str(send_result.get("reason") or "send_failed")
    if reason in DETERMINISTIC_DECLINE_REASONS:
        logger.warning(
            "approval execution send declined (deterministic) tenant=%s approval=%s reason=%s",
            user_id,
            approval_id,
            reason,
        )
        _mark_blocked(user_id, approval_id, "BLOCK", reason)
        return {
            "ok": True,
            "sent": False,
            "decision": "BLOCK",
            "reason": reason,
            "approval_id": approval_id,
            "retryable": False,
        }
    logger.warning(
        "approval execution send declined tenant=%s approval=%s reason=%s",
        user_id,
        approval_id,
        reason,
    )
    _set_status(user_id, approval_id, "approved")
    return {
        "ok": False,
        "sent": False,
        "decision": "ALLOW",
        "reason": reason,
        "approval_id": approval_id,
        "retryable": True,
    }


def process_execute_approval_job(job):
    """Worker handler for one ``execute_approval`` job.

    Payload: ``{"approval_id": <id>}``. Returns the worker result dict
    (see :func:`execute_approved`).
    """
    job = job or {}
    job_id = job.get("id")
    user_id = job.get("user_id")
    payload = (job or {}).get("payload")
    if payload is None:
        payload = (job or {}).get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    approval_id = payload.get("approval_id") or job.get("approval_id")
    if not user_id or not approval_id:
        logger.error(
            "execute_approval job missing fields job=%s tenant=%s approval=%s",
            job_id,
            user_id,
            approval_id,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": "missing_fields",
            "approval_id": approval_id,
            "retryable": False,
        }
    return execute_approved(
        user_id, approval_id, correlation_id=job.get("correlation_id")
    )
