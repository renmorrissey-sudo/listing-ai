"""Convert natural language into validated Phase 1 commands. Never executes them.

Confirmation-token flow — REMOVED (Phase I hardening, 2026-11-08)
------------------------------------------------------------------
Ask TopAI executes routine commands immediately: ``ask_topai.service.
interpret()`` runs the validated commands and returns no
``confirmation_token`` field at all.

The confirmation-token flow was removed, not kept, because it could
never succeed:

* ``interpret()`` never issued a token and never created a
  ``pending_confirmation`` row — every ``record_command()`` call site
  passes no token and a status other than ``pending_confirmation``.
* ``service.confirm()`` could therefore only ever return 400: its
  token lookup (``audit.get_by_token`` / ``get_pending_by_token``)
  matches only rows with a stored ``confirmation_token_hash``, and
  every row has a NULL hash — including executed rows, so even the
  duplicate-replay branch was unreachable. Minting a token alone
  granted nothing.
* No caller existed outside tests: the widget (``templates/
  ask_topai_widget.html``) only calls ``/api/ask-topai/interpret``
  and never reads ``confirmation_token``; nothing calls
  ``/api/ask-topai/confirm`` or ``/api/ask-topai/cancel`` (the latter
  also returned success unconditionally while cancelling nothing).

What was removed: ``confirmation_token`` from all ``interpret()``
responses, ``service.confirm()``, the ``/api/ask-topai/confirm`` and
``/api/ask-topai/cancel`` routes, ``audit.issue_confirmation_token()``,
``audit.get_by_token()``, ``audit.get_pending_by_token()``, and the
``confirmation_token`` / ``expires_at`` parameters of
``audit.record_command()``. The ``confirmation_token_hash`` and
``expires_at`` columns remain in ``ask_topai_commands`` (migration
023, already applied; dropping them needs a new migration) and now
always store NULL — harmless, no code reads them.

What would change this decision: if a future Ask TopAI action must
wait for a human before executing, build that confirmation step on
the harness human-approval flow (``harness.approvals`` /
``harness_approvals`` rows — the approval flow that actually
executes), not by resurrecting this token flow. The realtime
spoken-confirmation policy (``ask_topai.policy`` /
``realtime/runtime.py``) is separate and unaffected.

``tests/test_ask_topai_confirmation.py`` pins the removal (no
``confirmation_token`` key, no confirm/cancel routes, no issuer) and
the future-tool classification in ``ask_topai/registry.py``.
"""

from __future__ import annotations

import json
import logging
import re

from anthropic import Anthropic

import config
from ask_topai.schemas import (
    ALLOWED_ACTIONS,
    BLOCKED_ACTIONS,
    sanitize_command,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You convert a real-estate agent's spoken or typed request into JSON commands.
Return ONLY valid JSON, no markdown.
Allowed actions: create_lead, add_lead_note, create_task, update_property_criteria.
Never invent phone numbers, emails, lead IDs, prices, or property details that are not in the request.
Never output SQL or arbitrary function names.
Never choose send_sms, send_email, send_listings, place_call, delete_lead, or consent changes.
If required information is missing, set status to needs_clarification.
If several leads could match a name, set status to needs_clarification and do not guess.
If the request is unsupported, set status to unsupported.
You may return multiple commands in order when the user clearly asked for more than one safe CRM action.

Schema:
{
  "status": "ok" | "needs_clarification" | "unsupported",
  "message": "short agent-facing sentence",
  "commands": [
    {"action": "create_lead", "arguments": {}}
  ]
}
"""


def is_configured() -> bool:
    return bool((config.ANTHROPIC_API_KEY or "").strip()) and not str(
        config.ANTHROPIC_API_KEY
    ).startswith("test-")


def extract_json_object(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("Ask TopAI did not return JSON.")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Ask TopAI JSON must be an object.")
    return data


def call_llm(transcript: str, context: dict, *, user_id=None, correlation_id=None) -> dict:
    """Legacy single-shot Ask TopAI model call (pre-tool-loop parser).

    The live path is ``ask_topai.agent.complete`` (used by
    ``ask_topai.service.interpret`` and ``harness.orchestrator``); this
    function is only reached via ``interpret_request`` with no
    pre-supplied model payload, which has no in-repo callers. It is
    still gated and recorded like every other billable path: the spend
    cap is checked before the call (a tripped cap raises RuntimeError
    with no model call), and usage is recorded when ``user_id`` is
    given. Without a ``user_id`` the spend cannot be attributed to a
    tenant — that is a known gap, documented in
    docs/ai-cost-coverage.md.
    """
    if not is_configured():
        raise RuntimeError("Ask TopAI is not configured.")
    if user_id is not None:
        try:
            from harness.costs import is_spend_cap_tripped

            if is_spend_cap_tripped(user_id):
                raise RuntimeError(
                    "Monthly AI spend limit reached. Ask TopAI did not "
                    "run this request; the limit resets next month."
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # fail open on an unreadable ledger (see harness.costs)
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    context_bits = []
    if context.get("lead_id"):
        context_bits.append(f"selected_lead_id={context['lead_id']}")
    if context.get("lead_name"):
        context_bits.append(f"selected_lead_name={context['lead_name']}")
    if context.get("page"):
        context_bits.append(f"page={context['page']}")
    user_prompt = (
        f"Request: {transcript}\n"
        f"Safe context: {'; '.join(context_bits) or 'none'}\n"
        "Use selected_lead_id when the user says 'this lead', 'her', 'him', or omits the name."
    )
    message = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=900,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    if user_id is not None:
        try:
            from harness.costs import record_anthropic_message

            record_anthropic_message(
                user_id,
                "ask_topai_legacy",
                message,
                model=config.CLAUDE_MODEL,
                correlation_id=correlation_id,
            )
        except Exception:
            pass  # recording must never break the parse
    return extract_json_object(message.content[0].text)


def validate_model_payload(payload: dict, transcript: str) -> dict:
    status = str((payload or {}).get("status") or "ok").strip().lower()
    if status in {"informational", "informational_response"}:
        status = "informational"
    if status in {"clarification_required", "needs_clarification"}:
        status = "needs_clarification"
    if status in {"unsupported_action", "unsupported"}:
        status = "unsupported"
    message = str((payload or {}).get("message") or "").strip()
    if status == "informational":
        return {
            "status": "informational",
            "message": message or "I understood, and no CRM change is needed.",
            "commands": [],
        }
    if status == "error":
        return {
            "status": "error",
            "message": message or "Ask TopAI could not process that request.",
            "commands": [],
        }
    raw_commands = (payload or {}).get("commands")
    if raw_commands is None and payload.get("action"):
        raw_commands = [{"action": payload.get("action"), "arguments": payload.get("arguments") or {}}]
    if not isinstance(raw_commands, list):
        raw_commands = []

    commands = []
    for item in raw_commands:
        action = str((item or {}).get("action") or "").strip()
        if action in BLOCKED_ACTIONS or (action and action not in ALLOWED_ACTIONS):
            return {
                "status": "unsupported",
                "message": (
                    "Ask TopAI cannot do that yet. Phase 1 supports creating leads, "
                    "adding notes, creating tasks, and updating property criteria."
                ),
                "commands": [],
            }
        cleaned, err = sanitize_command(item, transcript)
        if cleaned is None:
            return {
                "status": "unsupported",
                "message": err or "That command is not allowed.",
                "commands": [],
            }
        commands.append({"command": cleaned, "error": err})

    if status == "unsupported":
        return {
            "status": "unsupported",
            "message": message or "Ask TopAI cannot do that yet.",
            "commands": [],
        }

    missing = [row["error"] for row in commands if row.get("error")]
    if status == "needs_clarification" or missing:
        return {
            "status": "needs_clarification",
            "message": message or (missing[0] if missing else "I need a bit more information."),
            "commands": [row["command"] for row in commands],
        }

    if not commands:
        return {
            "status": "needs_clarification",
            "message": message or "I did not understand a CRM action in that request.",
            "commands": [],
        }

    return {
        "status": "ok",
        "message": message or "I understood this request.",
        "commands": [row["command"] for row in commands],
    }


def interpret_request(transcript: str, context: dict | None = None, *, model_payload: dict | None = None,
                      user_id=None, correlation_id=None) -> dict:
    text = (transcript or "").strip()
    if not text:
        return {
            "status": "needs_clarification",
            "message": "Please say or type what you would like Ask TopAI to do.",
            "commands": [],
        }
    if model_payload is None:
        model_payload = call_llm(text, context or {}, user_id=user_id,
                                 correlation_id=correlation_id)
    return validate_model_payload(model_payload, text)
