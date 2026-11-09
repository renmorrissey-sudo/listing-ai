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
7. Token usage is recorded to ``harness.costs`` by the coach itself
   (``sms_coach.analyze_inbound_reply`` records exactly once, tool
   ``sms_coach``, with this job's correlation id) — the handler must
   NOT record it again or the same call would be double-counted.
   The monthly spend cap is enforced upstream in
   ``sms_inbound.analyze_inbound_and_coach``: a capped tenant never
   burns a coach call, and the job BLOCKs here with reason
   ``spend_cap_reached`` instead of drafting or sending.

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
    # Staging mode lock (fallback path — the get_settings path locks in
    # harness.policy): a staging process never reports a live mode.
    try:
        import config as _config

        if bool(getattr(_config, "IS_STAGING", False)):
            mode = DEFAULT_MODE
    except Exception:
        pass
    return mode, paused, ai_enabled


def _audit_raw(user_id, *, action, gate_decision, gate_reason, mode=None, target_type=None,
               target_id=None, job_id=None, cost_cents=None, correlation_id=None):
    """Raw-SQL harness_audit insert (fallback when harness.audit is absent).

    L4: ``correlation_id`` traces one inbound event end to end; when the
    caller does not supply it, the job id is used as before.
    """
    correlation_id = correlation_id or (
        str(job_id) if job_id is not None else None
    )
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
                correlation_id,
                _now(),
            ),
        )


def _audit(user_id, *, action, gate_decision, gate_reason, mode=None, target_type=None,
           target_id=None, job_id=None, cost_cents=None, correlation_id=None):
    """Append one harness_audit row. Best-effort: missing table => log only.

    Prefers the sibling-owned ``harness.audit.record_audit``; falls back to
    a raw-SQL insert with the same columns when that module is absent.

    L4: ``correlation_id`` traces one inbound event end to end; when the
    caller does not supply it, the job id is used as before.
    """
    correlation_id = correlation_id or (
        str(job_id) if job_id is not None else None
    )
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
            correlation_id=correlation_id,
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
            correlation_id=correlation_id,
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


def _is_rate_limited(user_id, phone):
    """Real tenant/contact rate check (sms_authorization._rate_limited:
    30/min, 500/hr, 2000/day per tenant, 3/contact/day, counted from
    outbound sms_messages rows). Fail closed: any error in the check
    itself is treated as rate-limited, never as a green light."""
    try:
        from sms_authorization import _rate_limited

        return bool(_rate_limited(user_id, phone))
    except Exception:
        logger.exception(
            "rate-limit check failed tenant=%s; failing closed", user_id
        )
        return True


def _load_inbound_body(user_id, inbound_id):
    """Load the raw inbound body from its sms_messages row (the source of
    truth). The job payload carries only a redacted fallback copy, so the
    handler prefers the stored row. Returns "" when the row is missing."""
    if not inbound_id:
        return ""
    try:
        row = db.get_sms_message(inbound_id, user_id)
    except Exception:
        logger.warning(
            "inbound sms_messages row unreadable tenant=%s inbound=%s",
            user_id,
            inbound_id,
        )
        return ""
    if not row:
        return ""
    return str(row.get("message_body") or "")


def _decide_policy(user_id, lead, analysis, draft, mode, paused, correlation_id=None):
    """Ask harness.policy for the send decision.

    Mode/paused are decided here first (deterministic, no model input):
    drafts_only or paused always means DRAFT. Otherwise the sibling-owned
    ``harness.policy.decide(user_id, action, tool, context)`` is consulted
    lazily; it returns ``(decision, reason)``. Any missing module or error
    defaults to DRAFT -- never to ALLOW.
    Returns (decision, reason, rate_limited) with decision in
    {ALLOW, DRAFT, BLOCK, DEFER}; rate_limited is the structured flag the
    caller uses to re-queue a rate-limit DEFER retryably (never keyed on
    the reason string, so a future policy rewording cannot silently
    change the retry behaviour).
    """
    if paused:
        return "DRAFT", "paused", False
    if mode == "drafts_only":
        return "DRAFT", "mode_drafts_only", False
    try:
        from harness.policy import decide
    except Exception:
        logger.warning("harness.policy unavailable; defaulting to DRAFT tenant=%s", user_id)
        return "DRAFT", "policy_unavailable", False
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
    context = {
        "mode": mode,
        "paused": paused,
        "consent_status": (lead or {}).get("sms_consent_status") or "",
        "opted_out": (lead or {}).get("opt_out_status") == "opted_out",
        "suppressed": suppressed,
        "quiet_hours": quiet_hours,
        # C2: consult the REAL tenant/contact rate check -- never a
        # hard-coded False. A tripped limit makes the policy DEFER, and
        # the caller re-queues that DEFER retryably (see below).
        "rate_limited": rate_limited,
        "escalation_topics": analysis.get("escalation_topics") or [],
        "analysis": analysis,
        "template_approved": False,
        # L4: the same correlation id the job, audit rows, and provider
        # execution log carry, so one inbound event traces end to end.
        "correlation_id": correlation_id,
    }
    try:
        result = decide(user_id, "send_sms", "send_sms", context, correlation_id=correlation_id)
    except Exception:
        logger.exception("harness.policy.decide failed tenant=%s", user_id)
        return "DRAFT", "policy_error", rate_limited
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
        return "DRAFT", "policy_unknown_decision", rate_limited
    return decision, reason or "policy_decision", rate_limited


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
    # L4: one correlation id for the whole trace — webhook -> job ->
    # policy -> audit -> provider execution. Jobs created before
    # migration 031 carry None and fall back to the job id in _audit.
    correlation_id = job.get("correlation_id")
    payload = _payload_dict(job)
    lead_id = payload.get("lead_id") or job.get("lead_id")
    inbound_id = payload.get("inbound_id") or job.get("inbound_id")
    # H3: prefer the raw body from the inbound sms_messages row (source of
    # truth); the payload only ever carries a redacted fallback copy.
    body = _load_inbound_body(user_id, inbound_id) or (
        payload.get("body") or payload.get("inbound_body") or ""
    )
    receiving_number = payload.get("receiving_number")

    # D5: strict reference validation before any work. A malformed id, a
    # nonexistent lead, or a lead belonging to another tenant fails the
    # job here — never silently, and never by coercing the reference.
    from harness.validation import ValidationError, validate_lead_reference

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
    try:
        lead = validate_lead_reference(user_id, lead_id, required=True)
    except ValidationError as exc:
        # lead_not_found / cross_tenant_lead / malformed_lead_id.
        logger.error(
            "sms_auto_reply lead validation failed job=%s tenant=%s lead=%s reason=%s",
            job_id,
            user_id,
            lead_id,
            exc.code,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": None,
            "reason": exc.code,
            "approval_id": None,
            "retryable": False,
        }

    mode, paused, _ai_enabled = get_harness_mode(user_id)

    # C2 (early): never burn a coach (Anthropic) call on a job that cannot
    # send right now. A tripped tenant/contact rate limit defers the job
    # retryably before any model call; _decide_policy re-checks at decision
    # time as defence in depth (a limit can trip between here and the send).
    # Drafts Only / paused short-circuit to DRAFT later and never send, so
    # they skip this check.
    if mode != "drafts_only" and not paused and _is_rate_limited(
        user_id, (lead or {}).get("phone_number") or ""
    ):
        _audit(
            user_id,
            action="sms_auto_reply_deferred",
            gate_decision="DEFER",
            gate_reason="tenant/per-contact rate limit reached; retry later",
            mode=mode,
            target_type="lead",
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
        )
        _needs_attention(user_id, lead_id, inbound_id, "ai_reply_deferred")
        logger.info(
            "sms_auto_reply DEFERRED by rate limit before coach (will retry) job=%s tenant=%s lead=%s",
            job_id,
            user_id,
            lead_id,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": "DEFER",
            "reason": "tenant/per-contact rate limit reached; retry later",
            "approval_id": None,
            "retryable": True,
        }

    # (2) Existing coach analysis -- never reimplemented here. The coach
    # records its own token usage to the cost ledger exactly once (tool
    # "sms_coach", this job's correlation id); a capped tenant never
    # reaches the model call — analyze_inbound_and_coach returns
    # error="spend_cap_reached" with no analysis, which BLOCKs below.
    from sms_inbound import analyze_inbound_and_coach

    coach = analyze_inbound_and_coach(
        user_id, lead_id, inbound_id, body, correlation_id=correlation_id
    ) or {}
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
            correlation_id=correlation_id,
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

    # (7) Token usage is already recorded exactly once by the coach
    # itself (sms_coach.analyze_inbound_reply, tool "sms_coach") —
    # nothing to record here; recording again would double-count.
    # A spend-cap trip surfaces as error="spend_cap_reached" with no
    # analysis and BLOCKs above with retryable=False (the cap resets
    # monthly; retrying with backoff would burn the job queue).

    # (3) Policy gate.
    decision, gate_reason, rate_limited = _decide_policy(
        user_id, lead, analysis, draft, mode, paused,
        correlation_id=correlation_id,
    )

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
            correlation_id=correlation_id,
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
        # D5: validate the coach's suggested lead status before it is
        # persisted in the approval draft. An invalid value is rejected
        # as a parameter — stripped from the draft and recorded in the
        # audit trail — never silently coerced to "new" by
        # normalize_lead_status downstream.
        from harness.validation import ValidationError, validate_status

        suggested_status = analysis.get("suggested_lead_status")
        invalid_suggested_status = None
        if suggested_status not in (None, ""):
            try:
                suggested_status = validate_status(suggested_status)
            except ValidationError:
                invalid_suggested_status = suggested_status
                suggested_status = None
        draft_reason = gate_reason
        if invalid_suggested_status is not None:
            draft_reason = (
                f"{gate_reason}; invalid suggested_lead_status "
                f"{invalid_suggested_status!r} rejected, not persisted"
            )
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
                "suggested_lead_status": suggested_status,
                "insight_id": coach.get("insight_id"),
            },
        )
        _audit(
            user_id,
            action="sms_auto_reply_drafted",
            gate_decision="DRAFT",
            gate_reason=draft_reason,
            mode=mode,
            target_type="lead",
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
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
            "reason": draft_reason,
            "approval_id": approval_id,
            "retryable": False,
        }

    if decision == "ALLOW":
        from sms_ai_agent import process_inbound_ai

        send_result = process_inbound_ai(
            user_id, lead_id, inbound_id, body, receiving_number
        ) or {}
        sent = bool(send_result.get("replied"))
        # L4: provider execution carries the same correlation id as the
        # job, policy decision, and audit rows.
        logger.info(
            "sms_auto_reply provider send correlation_id=%s job=%s tenant=%s lead=%s sent=%s",
            correlation_id,
            job_id,
            user_id,
            lead_id,
            sent,
        )
        _audit(
            user_id,
            action="sms_auto_reply_sent" if sent else "sms_auto_reply_send_failed",
            gate_decision="ALLOW",
            gate_reason=gate_reason,
            mode=mode,
            target_type="lead",
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
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
    # C2: a DEFER caused by a tripped rate limit is transient -- return
    # ok=False, retryable=True so the worker re-queues the job with
    # backoff instead of completing (dropping) or dead-lettering it.
    # Other DEFER causes (e.g. quiet hours) keep the existing behaviour:
    # ok=True, completed, no send. Keyed on the structured rate_limited
    # flag from _decide_policy, never on the reason string.
    if decision == "DEFER" and rate_limited:
        _audit(
            user_id,
            action="sms_auto_reply_deferred",
            gate_decision="DEFER",
            gate_reason=gate_reason,
            mode=mode,
            target_type="lead",
            target_id=lead_id,
            job_id=job_id,
            correlation_id=correlation_id,
        )
        _needs_attention(user_id, lead_id, inbound_id, "ai_reply_deferred")
        logger.info(
            "sms_auto_reply DEFERRED by rate limit (will retry) job=%s tenant=%s lead=%s",
            job_id,
            user_id,
            lead_id,
        )
        return {
            "ok": False,
            "sent": False,
            "decision": "DEFER",
            "reason": gate_reason,
            "approval_id": None,
            "retryable": True,
        }
    _audit(
        user_id,
        action="sms_auto_reply_blocked" if decision == "BLOCK" else "sms_auto_reply_deferred",
        gate_decision=decision,
        gate_reason=gate_reason,
        mode=mode,
        target_type="lead",
        target_id=lead_id,
        job_id=job_id,
            correlation_id=correlation_id,
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
