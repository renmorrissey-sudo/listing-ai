"""Harness SMS auto-reply job handler (Phase 1).

Processes one durable ``sms_auto_reply`` job:

1. Load the lead (user_id-scoped).
2. Run the existing coach analysis (``sms_inbound.analyze_inbound_and_coach``)
   -- analysis is never reimplemented here.
3. Ask the policy gate (``harness.policy.decide``, lazily imported) for the
   send decision. If the policy module is absent, default to DRAFT.
4. DRAFT: persist a ``harness_approvals`` row (status pending, kind
   ``sms_reply``) plus a ``harness_audit`` row and do NOT send.
5. ALLOW: call the existing ``sms_ai_agent.process_inbound_ai`` send path.
   Only reachable in supervised/authorized modes, which are not
   UI-selectable in Phase 1.
6. BLOCK/DEFER: audit the decision, create a needs-attention entry via the
   existing helper, no send.
7. Record Anthropic token usage to ``harness.costs`` when the coach analysis
   exposes usage (best-effort).

Hard rule: never send when the tenant mode is ``drafts_only`` or paused.
The mode gate is enforced locally here as well as by the policy module, so
a policy bug can never turn a Drafts Only tenant into a sender.

All harness-table writes are best-effort: if the Phase 1 harness tables do
not exist yet (migration 029 not applied), the write is logged and skipped
instead of failing the job -- but a missing approvals table in DRAFT mode
still means no send happens, which is the safe direction.

Every query is user_id-scoped; ``?`` placeholders throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import crm_db
import db

logger = logging.getLogger(__name__)

DEFAULT_MODE = "drafts_only"


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_harness_mode(user_id):
    """Return (mode, paused, ai_enabled) for a tenant.

    Prefers the sibling-owned ``harness.policy.get_settings`` (defaults
    drafts_only / not paused / AI enabled); falls back to a direct read of
    ``harness_settings`` and then to the same defaults when the policy
    module or table is not available yet. Phase 1 default is Drafts Only
    for everyone.
    """
    try:
        from harness.policy import get_settings

        settings = get_settings(user_id) or {}
        return (
            str(settings.get("mode") or DEFAULT_MODE).strip().lower() or DEFAULT_MODE,
            bool(settings.get("paused")),
            bool(settings.get("ai_enabled", True)),
        )
    except Exception:
        pass
    mode, paused, ai_enabled = DEFAULT_MODE, False, True
    try:
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT mode, paused, ai_enabled FROM harness_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    except Exception:
        logger.debug("harness_settings unreadable for tenant=%s; using defaults", user_id)
        row = None
    if row:
        mode = (row["mode"] or DEFAULT_MODE).strip().lower() or DEFAULT_MODE
        paused = bool(row["paused"])
        ai_enabled = bool(row["ai_enabled"]) if row["ai_enabled"] is not None else True
    return mode, paused, ai_enabled


def _audit_raw(user_id, *, action, gate_decision, gate_reason, mode=None, target_type=None,
               target_id=None, job_id=None, cost_cents=None):
    """Raw-SQL harness_audit insert (fallback when harness.audit is absent)."""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO harness_audit
                (user_id, actor, action, tool, target_type, target_id,
                 mode, gate_decision, gate_reason, cost_cents,
                 request_id, correlation_id, created_at)
            VALUES (?, 'ai', ?, 'sms_auto_reply', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                action,
                target_type,
                target_id,
                mode,
                gate_decision,
                (gate_reason or "")[:500],
                cost_cents,
                str(job_id) if job_id is not None else None,
                str(job_id) if job_id is not None else None,
                _now(),
            ),
        )


def _audit(user_id, *, action, gate_decision, gate_reason, mode=None, target_type=None,
           target_id=None, job_id=None, cost_cents=None):
    """Append one harness_audit row. Best-effort: missing table => log only.

    Prefers the sibling-owned ``harness.audit.record_audit``; falls back to
    a raw-SQL insert with the same columns when that module is absent.
    """
    try:
        from harness.audit import record_audit

        record_audit(
            user_id,
            "ai",
            action,
            tool="sms_auto_reply",
            target_type=target_type,
            target_id=target_id,
            mode=mode,
            gate_decision=gate_decision,
            gate_reason=(gate_reason or "")[:500],
            cost_cents=cost_cents or 0,
            request_id=str(job_id) if job_id is not None else None,
            correlation_id=str(job_id) if job_id is not None else None,
        )
        return
    except Exception:
        pass
    try:
        _audit_raw(
            user_id,
            action=action,
            gate_decision=gate_decision,
            gate_reason=gate_reason,
            mode=mode,
            target_type=target_type,
            target_id=target_id,
            job_id=job_id,
            cost_cents=cost_cents,
        )
    except Exception:
        logger.warning(
            "harness_audit write failed tenant=%s job=%s (table may not exist yet)",
            user_id,
            job_id,
        )


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
                (user_id, job_id, kind, json.dumps(draft_payload), _now()),
            )
            return cur.lastrowid
    except Exception:
        logger.warning(
            "harness_approvals write failed tenant=%s job=%s (table may not exist yet)",
            user_id,
            job_id,
        )
        return None


def _record_cost(user_id, usage, *, job_id=None):
    """Record Anthropic token usage to harness_costs, best-effort.

    The coach analysis exposes a ``usage`` payload (sms_coach attaches the
    Anthropic response usage fields since Phase 1); without one this is a
    no-op. Never raises.
    Prefers the sibling-owned ``harness.costs.record_anthropic_usage``.
    """
    if not isinstance(usage, dict):
        return None
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    total = input_tokens + output_tokens
    if total <= 0:
        return None
    try:
        import config
        from harness.costs import record_anthropic_usage

        record_anthropic_usage(
            user_id, "sms_auto_reply", usage, model=config.CLAUDE_MODEL
        )
        return total
    except Exception:
        pass
    try:
        from datetime import datetime as _dt

        period = _dt.now(timezone.utc).strftime("%Y-%m")
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO harness_costs
                    (user_id, period, provider, tool, units, unit_type,
                     cost_cents, created_at)
                VALUES (?, ?, 'anthropic', 'sms_auto_reply', ?, 'tokens', 0, ?)
                """,
                (user_id, period, total, _now()),
            )
        return total
    except Exception:
        logger.warning("harness_costs write failed tenant=%s job=%s", user_id, job_id)
        return None


def _needs_attention(user_id, lead_id, inbound_id, reason_code):
    """Create a needs-attention entry via the existing CRM helper."""
    try:
        crm_db.upsert_needs_attention(
            user_id,
            lead_id,
            reason_code,
            priority="high",
            source_ref_type="sms",
            source_ref_id=inbound_id,
        )
    except Exception:
        logger.exception(
            "Failed to flag needs attention lead_id=%s tenant=%s", lead_id, user_id
        )


def _decide_policy(user_id, lead, analysis, draft, mode, paused):
    """Ask harness.policy for the send decision.

    Mode/paused are decided here first (deterministic, no model input):
    drafts_only or paused always means DRAFT. Otherwise the sibling-owned
    ``harness.policy.decide(user_id, action, tool, context)`` is consulted
    lazily; it returns ``(decision, reason)``. Any missing module or error
    defaults to DRAFT -- never to ALLOW.
    Returns (decision, reason) with decision in {ALLOW, DRAFT, BLOCK, DEFER}.
    """
    if paused:
        return "DRAFT", "paused"
    if mode == "drafts_only":
        return "DRAFT", "mode_drafts_only"
    try:
        from harness.policy import decide
    except Exception:
        logger.warning("harness.policy unavailable; defaulting to DRAFT tenant=%s", user_id)
        return "DRAFT", "policy_unavailable"
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
    context = {
        "mode": mode,
        "paused": paused,
        "consent_status": (lead or {}).get("sms_consent_status") or "",
        "opted_out": (lead or {}).get("opt_out_status") == "opted_out",
        "suppressed": suppressed,
        "quiet_hours": quiet_hours,
        "rate_limited": False,
        "escalation_topics": analysis.get("escalation_topics") or [],
        "analysis": analysis,
        "template_approved": False,
    }
    try:
        result = decide(user_id, "send_sms", "send_sms", context)
    except Exception:
        logger.exception("harness.policy.decide failed tenant=%s", user_id)
        return "DRAFT", "policy_error"
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
        return "DRAFT", "policy_unknown_decision"
    return decision, reason or "policy_decision"


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


def process_sms_auto_reply_job(job):
    """Handle one ``sms_auto_reply`` harness job. Returns a result dict.

    Result keys: ok, sent, decision, reason, approval_id, retryable.
    ``ok=False, retryable=False`` means permanent failure (worker should
    dead-letter, never silently drop). ``ok=False, retryable=True`` means a
    transient failure worth retrying with backoff.
    """
    job = job or {}
    job_id = job.get("id")
    user_id = job.get("user_id")
    payload = _payload_dict(job)
    lead_id = payload.get("lead_id") or job.get("lead_id")
    inbound_id = payload.get("inbound_id") or job.get("inbound_id")
    body = payload.get("body") or payload.get("inbound_body") or ""
    receiving_number = payload.get("receiving_number")

    if not user_id or not lead_id or not inbound_id:
        logger.error(
            "sms_auto_reply job missing fields job=%s tenant=%s lead=%s inbound=%s",
            job_id,
            user_id,
            lead_id,
            inbound_id,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": "missing_fields",
            "approval_id": None,
            "retryable": False,
        }

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        logger.error(
            "sms_auto_reply lead missing job=%s tenant=%s lead=%s", job_id, user_id, lead_id
        )
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": "lead_missing",
            "approval_id": None,
            "retryable": False,
        }

    mode, paused, _ai_enabled = get_harness_mode(user_id)

    # (2) Existing coach analysis -- never reimplemented here.
    from sms_inbound import analyze_inbound_and_coach

    coach = analyze_inbound_and_coach(user_id, lead_id, inbound_id, body) or {}
    analysis = coach.get("analysis")
    if coach.get("error") == "coach_failed" or not analysis:
        reason = coach.get("error") or "no_analysis"
        logger.error(
            "sms_auto_reply coach failed job=%s tenant=%s lead=%s reason=%s",
            job_id,
            user_id,
            lead_id,
            reason,
        )
        _needs_attention(user_id, lead_id, inbound_id, "ai_reply_failed")
        _audit(
            user_id,
            action="sms_auto_reply_coach_failed",
            gate_decision="BLOCK",
            gate_reason=reason,
            mode=mode,
            target_type="lead",
            target_id=lead_id,
            job_id=job_id,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": "BLOCK",
            "reason": reason,
            "approval_id": None,
            # Transient provider/model failure: retry with backoff.
            "retryable": coach.get("error") == "coach_failed",
        }

    draft = str(analysis.get("draft_reply") or analysis.get("suggested_reply") or "").strip()[:480]

    # (7) Token usage, best-effort (coach exposes usage since Phase 1).
    _record_cost(user_id, coach.get("usage") or analysis.get("usage"), job_id=job_id)

    # (3) Policy gate.
    decision, gate_reason = _decide_policy(user_id, lead, analysis, draft, mode, paused)

    # Defense in depth: escalation topics can never auto-send, even if the
    # policy module returned ALLOW.
    escalation = set(analysis.get("escalation_topics") or [])
    if decision == "ALLOW" and (analysis.get("sensitive_topic") or escalation):
        decision, gate_reason = "DRAFT", "escalation_topic"
    # Hard rule, re-checked immediately before any send path: drafts_only or
    # paused can never reach the provider, whatever the policy said.
    if decision == "ALLOW" and (mode == "drafts_only" or paused):
        decision, gate_reason = "DRAFT", "allow_overridden_drafts_only_or_paused"

    if decision == "DRAFT":
        if not draft:
            _audit(
                user_id,
                action="sms_auto_reply_drafted",
                gate_decision="DRAFT",
                gate_reason="empty_draft",
                mode=mode,
                target_type="lead",
                target_id=lead_id,
                job_id=job_id,
            )
            _needs_attention(user_id, lead_id, inbound_id, "ai_reply_failed")
            return {
                "ok": True,
                "sent": False,
                "decision": "DRAFT",
                "reason": "empty_draft",
                "approval_id": None,
                "retryable": False,
            }
        approval_id = _create_approval(
            user_id,
            job_id,
            "sms_reply",
            {
                "draft_reply": draft,
                "lead_id": lead_id,
                "inbound_id": inbound_id,
                "intent": analysis.get("intent"),
                "summary": analysis.get("summary"),
                "suggested_lead_status": analysis.get("suggested_lead_status"),
                "insight_id": coach.get("insight_id"),
            },
        )
        _audit(
            user_id,
            action="sms_auto_reply_drafted",
            gate_decision="DRAFT",
            gate_reason=gate_reason,
            mode=mode,
            target_type="lead",
            target_id=lead_id,
            job_id=job_id,
        )
        logger.info(
            "sms_auto_reply drafted (no send) job=%s tenant=%s lead=%s approval=%s",
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

    if decision == "ALLOW":
        from sms_ai_agent import process_inbound_ai

        send_result = process_inbound_ai(
            user_id, lead_id, inbound_id, body, receiving_number
        ) or {}
        sent = bool(send_result.get("replied"))
        _audit(
            user_id,
            action="sms_auto_reply_sent" if sent else "sms_auto_reply_send_failed",
            gate_decision="ALLOW",
            gate_reason=gate_reason,
            mode=mode,
            target_type="lead",
            target_id=lead_id,
            job_id=job_id,
        )
        return {
            "ok": True,
            "sent": sent,
            "decision": "ALLOW",
            "reason": send_result.get("reason") or gate_reason,
            "approval_id": None,
            "retryable": False,
            "message_id": send_result.get("message_id"),
        }

    # BLOCK / DEFER: audit, flag for human attention, no send.
    _audit(
        user_id,
        action="sms_auto_reply_blocked" if decision == "BLOCK" else "sms_auto_reply_deferred",
        gate_decision=decision,
        gate_reason=gate_reason,
        mode=mode,
        target_type="lead",
        target_id=lead_id,
        job_id=job_id,
    )
    _needs_attention(
        user_id,
        lead_id,
        inbound_id,
        "ai_reply_blocked" if decision == "BLOCK" else "ai_reply_deferred",
    )
    logger.info(
        "sms_auto_reply %s (no send) job=%s tenant=%s lead=%s reason=%s",
        decision,
        job_id,
        user_id,
        lead_id,
        gate_reason,
    )
    return {
        "ok": True,
        "sent": False,
        "decision": decision,
        "reason": gate_reason,
        "approval_id": None,
        "retryable": False,
    }
