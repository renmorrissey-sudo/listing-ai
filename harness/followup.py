"""Scheduled follow-up job handler (H4).

Handles one durable ``scheduled_follow_up`` job:

1. Load the lead (user_id-scoped). A missing lead is permanent --
   ``ok=False, retryable=False`` so the worker dead-letters the job.
2. Create a real CRM follow-up via the same path Ask TopAI uses
   (``ask_topai.actions.execute_create_follow_up`` -> ``crm_db.
   set_lead_follow_up``); never reimplemented here. ``due_at`` defaults
   to 24h from now when the payload carries none, because
   ``set_lead_follow_up`` requires one.
3. Write one ``harness_audit`` row (best-effort) and return ok=True.

Hard rule: this handler NEVER sends SMS or email. Sending goes through
the gated ``sms_auto_reply`` path only; a follow-up is a CRM record
(lead_follow_ups + lead activity), not a message.

Result keys mirror harness.sms_handler: ok, sent (always False),
decision, reason, follow_up_id, retryable. ``ok=False, retryable=False``
means permanent failure (worker should dead-letter, never silently drop).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _default_due_at():
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def _payload_dict(job):
    import json

    payload = (job or {}).get("payload")
    if payload is None:
        payload = (job or {}).get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _audit(user_id, *, action, mode, lead_id, job_id, gate_reason):
    """Append one harness_audit row. Best-effort: missing table => log only."""
    try:
        from harness.audit import record_audit

        record_audit(
            user_id,
            "ai",
            action,
            tool="scheduled_follow_up",
            target_type="lead",
            target_id=lead_id,
            mode=mode,
            gate_decision=None,
            gate_reason=(gate_reason or "")[:500],
            cost_cents=0,
            request_id=str(job_id) if job_id is not None else None,
            correlation_id=str(job_id) if job_id is not None else None,
        )
    except Exception:
        logger.warning(
            "scheduled_follow_up audit write failed tenant=%s job=%s",
            user_id,
            job_id,
        )


def _create_follow_up(user_id, lead_id, due_at, reason, priority):
    """Create the CRM follow-up via the Ask TopAI path.

    Returns (follow_up_id, error). Prefers
    ``ask_topai.actions.execute_create_follow_up`` (which also writes the
    lead audit entry); falls back to ``crm_db.set_lead_follow_up``
    directly if that module is unavailable.
    """
    arguments = {
        "lead_id": lead_id,
        "due_at": due_at,
        "reason": reason,
        "priority": priority,
    }
    try:
        from ask_topai.actions import execute_create_follow_up

        result, error, _choices = execute_create_follow_up(user_id, arguments, None)
        if error:
            return None, str(error)
        if not result:
            return None, "follow-up creation returned no result"
        return result.get("follow_up_id"), None
    except Exception:
        logger.warning(
            "ask_topai.actions.execute_create_follow_up unavailable; "
            "falling back to crm_db.set_lead_follow_up tenant=%s lead=%s",
            user_id,
            lead_id,
        )
    try:
        import crm_db

        result, error = crm_db.set_lead_follow_up(
            user_id, lead_id, due_at, reason,
            priority=priority, created_by=user_id,
        )
        if error:
            return None, str(error)
        if not result:
            return None, "follow-up creation returned no result"
        return result.get("follow_up_id"), None
    except Exception as exc:
        logger.exception(
            "scheduled_follow_up creation failed tenant=%s lead=%s",
            user_id,
            lead_id,
        )
        return None, str(exc)


def process_scheduled_follow_up_job(job):
    """Handle one ``scheduled_follow_up`` harness job. Returns a result dict.

    Never sends SMS/email. A missing lead (or missing lead_id) is a
    permanent failure: ok=False, retryable=False.
    """
    job = job or {}
    job_id = job.get("id")
    user_id = job.get("user_id")
    payload = _payload_dict(job)
    lead_id = payload.get("lead_id") or job.get("lead_id")

    if not user_id or not lead_id:
        logger.error(
            "scheduled_follow_up job missing fields job=%s tenant=%s lead=%s",
            job_id,
            user_id,
            lead_id,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": "missing_fields",
            "follow_up_id": None,
            "retryable": False,
        }

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        logger.error(
            "scheduled_follow_up lead missing job=%s tenant=%s lead=%s",
            job_id,
            user_id,
            lead_id,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": "lead_missing",
            "follow_up_id": None,
            "retryable": False,
        }

    due_at = (
        payload.get("due_at")
        or payload.get("due_date")
        or payload.get("scheduled_for")
        or _default_due_at()
    )
    reason = (
        payload.get("reason")
        or payload.get("note")
        or payload.get("message")
        or "Follow up"
    )
    priority = payload.get("priority") or "normal"

    follow_up_id, error = _create_follow_up(
        user_id, lead_id, due_at, reason, priority
    )
    if error:
        logger.error(
            "scheduled_follow_up creation failed job=%s tenant=%s lead=%s reason=%s",
            job_id,
            user_id,
            lead_id,
            error,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": error,
            "follow_up_id": None,
            "retryable": False,
        }

    mode = None
    try:
        from harness.policy import get_settings

        mode = (get_settings(user_id) or {}).get("mode")
    except Exception:
        mode = None
    _audit(
        user_id,
        action="scheduled_follow_up_created",
        mode=mode,
        lead_id=lead_id,
        job_id=job_id,
        gate_reason=f"follow_up_id={follow_up_id} due_at={due_at}",
    )
    logger.info(
        "scheduled_follow_up created job=%s tenant=%s lead=%s follow_up=%s",
        job_id,
        user_id,
        lead_id,
        follow_up_id,
    )
    return {
        "ok": True,
        "sent": False,
        "decision": "CREATED",
        "reason": "follow_up_created",
        "follow_up_id": follow_up_id,
        "retryable": False,
    }
