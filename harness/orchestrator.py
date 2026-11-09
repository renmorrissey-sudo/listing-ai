"""Harness orchestrator — plan / select / verify, never send.

Wraps the existing Ask TopAI Claude tool-use loop
(:func:`ask_topai.agent.complete`) — no new model vendor, no OpenAI Agents
SDK in Python. The loop's read tools execute in-loop (read-only,
``user_id``-scoped, as today); its write tools are only *queued* as
proposed commands by the loop itself. This orchestrator never executes a
write: every proposed command is passed through
:func:`harness.policy.decide` first, and the result is a plan + draft for a
human (or an approved job) to act on.

``run(user_id, text, context=None)`` returns a dict with keys::

    plan           — steps, proposed tools, approval requirement, model status
    gate_decision  — aggregate "ALLOW" | "DRAFT" | "BLOCK" | "DEFER"
    gate_reason    — human-readable reason for the aggregate decision
    draft          — proposed message + commands, or None when nothing proposed
    tool_calls     — per-command [{action, arguments, decision, reason}]
    audit_id       — harness_audit row id, or None (audit module pending)

The Anthropic client is built lazily inside ``agent.complete`` (via
``ask_topai.claude``); importing this module requires no live Anthropic key.
Audit recording is also lazy: if ``harness/audit.py`` does not exist yet,
the call is skipped behind a try/except ImportError and ``audit_id`` is
None (the data-layer agent owns ``harness.audit``).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _record_audit(user_id, action, *, tool=None, gate_decision=None,
                  gate_reason=None, request_text=None, correlation_id=None):
    """Best-effort harness audit write. Returns the audit row id or None.

    ``harness.audit`` is owned by the data-layer agent; until it exists this
    is a silent no-op. Several candidate entry points are tried in order so
    the orchestrator does not depend on the audit module's exact function
    name — the first callable found wins, and any failure is logged, never
    raised.

    L4: ``correlation_id`` traces one orchestrator run end to end.
    """
    try:
        from harness import audit
    except ImportError:
        return None
    payload = {
        "user_id": user_id,
        "actor": "ai",
        "action": action,
        "tool": tool,
        "gate_decision": gate_decision,
        "gate_reason": gate_reason,
        "correlation_id": correlation_id,
    }
    for name in ("record_audit", "record", "record_event", "log_event", "append", "write"):
        fn = getattr(audit, name, None)
        if callable(fn):
            try:
                result = fn(**payload)
            except TypeError:
                try:
                    result = fn(user_id, action, gate_decision, gate_reason)
                except Exception as exc:
                    logger.warning("harness audit %s failed: %s", name, exc)
                    return None
            except Exception as exc:
                logger.warning("harness audit %s failed: %s", name, exc)
                return None
            if isinstance(result, dict):
                return result.get("id") or result.get("audit_id")
            return result if isinstance(result, int) else None
    return None


def _sanitize_context(user_id, context):
    """Tenant-scope the caller context via Ask TopAI's sanitizer if present."""
    raw = dict(context or {})
    try:
        from ask_topai import service

        return service.sanitize_context(user_id, raw)
    except Exception:
        lead_id = raw.get("lead_id")
        try:
            lead_id = int(lead_id) if lead_id not in (None, "") else None
        except (TypeError, ValueError):
            lead_id = None
        return {
            "page": str(raw.get("page") or "")[:200],
            "lead_id": lead_id,
            "lead_name": raw.get("lead_name"),
        }


def _aggregate_decision(tool_calls):
    """Aggregate per-command decisions: BLOCK > DEFER > DRAFT > ALLOW."""
    decisions = [c.get("decision") for c in tool_calls]
    if "BLOCK" in decisions:
        blocked = [c for c in tool_calls if c.get("decision") == "BLOCK"]
        return "BLOCK", blocked[0].get("reason") or "a proposed action is restricted"
    if "DEFER" in decisions:
        deferred = [c for c in tool_calls if c.get("decision") == "DEFER"]
        return "DEFER", deferred[0].get("reason") or "a proposed action must wait"
    if "DRAFT" in decisions:
        drafted = [c for c in tool_calls if c.get("decision") == "DRAFT"]
        return "DRAFT", drafted[0].get("reason") or "proposed actions need human approval"
    return "ALLOW", "no proposed action requires approval"


def run(user_id, text, context=None) -> dict:
    """Plan a response to *text* for *user_id* without executing writes.

    Steps: (1) deterministic prompt-injection screen; (2) Ask TopAI Claude
    loop proposes commands (reads execute in-loop, writes only queued);
    (3) every proposed command is gated by ``harness.policy.decide``;
    (4) the plan + draft + per-command gate results are returned, and the
    run is recorded in the harness audit log when available.
    """
    from harness import policy

    text = str(text or "").strip()
    context = _sanitize_context(user_id, context)
    # L4: one correlation id for the whole run — injection screen, model
    # call, per-command policy decisions, and audit rows all carry it.
    from harness.jobs import new_correlation_id

    correlation_id = new_correlation_id()
    context = dict(context)
    context["correlation_id"] = correlation_id

    # 1. Injection screen — deterministic, before any model call.
    if policy.screen_inbound_text(text):
        audit_id = _record_audit(
            user_id, "orchestrator.run",
            gate_decision="BLOCK",
            gate_reason="inbound text flagged by prompt-injection screen",
            correlation_id=correlation_id,
        )
        return {
            "plan": {
                "steps": ["screen_inbound_text"],
                "tools": [],
                "requires_approval": True,
                "suspicious": True,
                "status": "blocked",
                "model": None,
            },
            "gate_decision": "BLOCK",
            "gate_reason": "inbound text flagged by prompt-injection screen; no tool was called",
            "draft": None,
            "tool_calls": [],
            "audit_id": audit_id,
            "correlation_id": correlation_id,
        }

    # 2. Ask TopAI loop — proposes commands; never executes writes itself.
    try:
        from ask_topai import agent

        raw = agent.complete(user_id, text, context)
    except Exception as exc:
        logger.warning("harness orchestrator model call failed: %s", exc)
        audit_id = _record_audit(
            user_id, "orchestrator.run",
            gate_decision="DEFER",
            gate_reason=f"model unavailable: {exc}",
            correlation_id=correlation_id,
        )
        return {
            "plan": {
                "steps": ["screen_inbound_text", "agent.complete"],
                "tools": [],
                "requires_approval": False,
                "suspicious": False,
                "status": "error",
                "model": None,
            },
            "gate_decision": "DEFER",
            "gate_reason": f"model unavailable, retry later: {exc}",
            "draft": None,
            "tool_calls": [],
            "audit_id": audit_id,
            "correlation_id": correlation_id,
        }

    status = raw.get("status") or "error"
    message = raw.get("message") or ""
    commands = raw.get("commands") or []
    invoked = raw.get("tools_invoked") or []

    # 3. Gate every proposed write/send through policy before anything acts.
    # D5: validate write-tool arguments first — an invalid command is
    # BLOCKed in the preview, never silently fixed up or passed along.
    tool_calls = []
    gate_context = dict(context)
    for command in commands:
        action = str(command.get("action") or "").strip()
        arguments = command.get("arguments") or {}
        validation_error = None
        try:
            from harness import validation as _validation

            arguments = _validation.validate_tool_arguments(
                user_id, action, arguments
            )
        except _validation.ValidationError as exc:
            validation_error = f"validation failed ({exc.code}): {exc.message}"
        except Exception as exc:  # validator unavailable: fail closed
            validation_error = f"validation unavailable: {exc}"
        if validation_error is not None:
            tool_calls.append(
                {
                    "action": action,
                    "arguments": arguments,
                    "decision": "BLOCK",
                    "reason": validation_error,
                }
            )
            continue
        decision, reason = policy.decide(user_id, action, action, context=gate_context)
        tool_calls.append(
            {
                "action": action,
                "arguments": arguments,
                "decision": decision,
                "reason": reason,
            }
        )

    if status == "error":
        gate_decision, gate_reason = "DEFER", message or "model error"
    elif not tool_calls:
        gate_decision, gate_reason = "ALLOW", "informational response; no action proposed"
    else:
        gate_decision, gate_reason = _aggregate_decision(tool_calls)

    requires_approval = gate_decision in {"DRAFT", "BLOCK", "DEFER"} or bool(tool_calls)
    draft = None
    if tool_calls or message:
        draft = {
            "message": message,
            "commands": [
                {"action": c["action"], "arguments": c["arguments"]} for c in tool_calls
            ],
            "requires_approval": requires_approval,
        }

    plan = {
        "steps": ["screen_inbound_text", "agent.complete", "policy.decide"],
        "tools": invoked,
        "requires_approval": requires_approval,
        "suspicious": False,
        "status": status,
        "model": raw.get("model"),
        "session_id": raw.get("session_id"),
    }

    audit_id = _record_audit(
        user_id, "orchestrator.run",
        gate_decision=gate_decision,
        gate_reason=gate_reason,
        correlation_id=correlation_id,
    )

    return {
        "plan": plan,
        "gate_decision": gate_decision,
        "gate_reason": gate_reason,
        "draft": draft,
        "tool_calls": tool_calls,
        "audit_id": audit_id,
            "correlation_id": correlation_id,
    }
