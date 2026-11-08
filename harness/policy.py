"""Deterministic harness policy gate.

Extends :mod:`autonomy` — it is imported and reused, never forked or copied.
Every decision here is deterministic code: consent, suppression, quiet hours,
mode, escalation topics, restricted actions. No model input is consulted, and
model output is never trusted.

Decision vocabulary (``decide()`` returns ``(decision, reason)``):

* ``ALLOW`` — the proposed action may proceed.
* ``DRAFT`` — do not execute; produce a draft / approval item for a human.
* ``BLOCK`` — refuse outright (restricted action, missing/revoked consent,
  suppression / opt-out / do-not-contact).
* ``DEFER`` — do not act now; schedule for later (quiet hours, rate limit).

Mode semantics (``harness_settings.mode``; new tenants default to
``drafts_only``):

* ``drafts_only`` — every write/send becomes a draft. Reads are allowed.
* ``supervised`` — routine reads/writes allowed; sends only when the draft
  uses an approved template (``context["template_approved"]``).
* ``authorized`` — routine reads/writes and routine sends allowed; sensitive
  actions still draft, restricted actions still block.
"""

from __future__ import annotations

import logging
import re

import autonomy

logger = logging.getLogger(__name__)

MODES = ("drafts_only", "supervised", "authorized")

DEFAULT_MODE = "drafts_only"

DEFAULT_SETTINGS = {
    "mode": DEFAULT_MODE,
    "paused": False,
    "ai_enabled": True,
}

# Decisions returned by decide().
DECISIONS = ("ALLOW", "DRAFT", "BLOCK", "DEFER")

# Actions/tools that are never auto-executable in any mode. Extends (never
# replaces) autonomy.BLOCK_TOOLS with the harness restricted-action list from
# the AI Harness plan §4.4: contract negotiation, binding finance, legal
# advice, fabricating property info, refunds / subscription cancellation,
# consent changes, and any send the mode does not allow.
RESTRICTED_ACTIONS = frozenset(
    {
        "negotiate_contract",
        "negotiate",
        "binding_finance",
        "make_financial_commitment",
        "legal_advice",
        "fabricate_property_info",
        "invent_property_info",
        "refund",
        "issue_refund",
        "cancel_subscription",
        "cancel_plan",
        # D3: subscription changes and customer deletion are never
        # auto-executed (harness.tools also classifies them restricted,
        # so the registry/policy agreement check covers them).
        "change_subscription",
        "modify_subscription",
        "update_subscription",
        "change_plan",
        "update_plan",
        "delete_customer",
        "change_consent",
        "change_sms_qualification",
        "delete_lead",
        "delete_records",
        "bulk_action",
        "sql",
        "execute_sql",
        "raw_query",
    }
)

# Tools that are restricted in EVERY surface and EVERY mode (plan §4.3:
# the Vapi divergence is resolved here, not per surface). A generic send
# (send_sms / send_email / initiate_ai_call) is mode-gated, not in this
# set; these two are never auto-executed, even in "authorized" mode.
#
# Registry/policy seam (H2 reconciliation): this set and
# harness.tools risk_class "restricted" are two views of the same rule —
# "restricted" in the registry ⟺ decide() BLOCKs in all three modes.
# Generic sends are risk_class "sensitive" in the registry (mode-gated,
# so authorized autonomy may allow them); only send_lead_email — a real
# SendGrid send — stays restricted here and in the registry. Keep the
# two in agreement; tests/test_harness_policy_gate.py asserts it for
# every registry tool.
ALWAYS_BLOCK_TOOLS = frozenset(
    {
        "send_lead_email",  # real SendGrid send from the Vapi surface
        "update_lead_sms_consent_status",  # consent-changing tool
        "change_consent",
        "change_sms_qualification",
    }
)
# autonomy.BLOCK_TOOLS already lists the send family; keep the harness set
# explicit so a future registry change cannot silently unblock a send.
SEND_TOOLS = frozenset(
    {
        "send_sms",
        "send_email",
        "send_lead_email",
        "initiate_ai_call",
        "place_call",
        "start_call",
        "send_listings",
    }
)

SEND_ACTIONS = frozenset(
    {
        "send_sms",
        "send_email",
        "send_lead_email",
        "initiate_ai_call",
        "place_call",
        "start_call",
        "send_listings",
        "draft_sms_send",
        "sms_send",
        "email_send",
        "ai_call",
    }
)

# Write tools that only create a draft (no provider call). These are
# sensitive but not sends; in drafts_only they are the *output*, not an
# action to gate — decide() still returns DRAFT for them so callers route
# them to the approvals queue rather than executing anything.
DRAFT_TOOLS = frozenset({"draft_sms", "draft_email", "draft_lead_email"})

# Consent states that forbid sending (mirrors autonomy.should_auto_send_sms).
CONSENT_BLOCKED_STATES = frozenset(
    {
        "opted_out",
        "revoked",
        "not_permitted",
        "suppressed",
        "invalid_number",
    }
)

# Escalation topics come from autonomy — reused, not duplicated.
ESCALATION_TOPICS = autonomy.SMS_ESCALATION_BLOCK

# --- Prompt-injection screen (deterministic, keyword/regex only) -----------

_INJECTION_PATTERNS = [
    # Direct instruction-override attempts.
    re.compile(r"ignore\s+(all\s+)?(your|the|these|prior|previous)\s+instructions?", re.I),
    re.compile(r"disregard\s+(all\s+)?(your|the|these|prior|previous)\s+instructions?", re.I),
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+prompts?", re.I),
    re.compile(r"override\s+(your|the)\s+instructions?", re.I),
    re.compile(r"forget\s+(your|the|all)\s+instructions?", re.I),
    # Prompt / instruction exfiltration.
    re.compile(r"(reveal|show|print|repeat|disclose|dump)\s+(me\s+)?(your|the)\s+(system\s+)?prompt", re.I),
    re.compile(r"what\s+(are|is)\s+your\s+(system\s+)?(instructions?|prompt)", re.I),
    re.compile(r"(reveal|show|print|repeat|disclose)\s+(your|the)\s+instructions?", re.I),
    # Directing outbound sends / exfiltration to third parties.
    re.compile(r"send\s+(an?\s+)?(sms|text|email|message)\s+to\s+(everyone|everybody|all|all\s+leads|all\s+contacts)", re.I),
    re.compile(r"send\s+(sms|text|email)\s+to\s+all\b", re.I),
    re.compile(r"(text|message|email)\s+(everyone|everybody|all\s+leads|all\s+contacts)", re.I),
    re.compile(r"transfer\s+(money|funds|payment)", re.I),
    re.compile(r"exfiltrat\w*", re.I),
    re.compile(r"(send|leak|share|forward|upload)\s+.*\b(all\s+)?(lead|contact|customer)\s+(data|list|phone|email)", re.I),
    # Jailbreak / role-play override markers.
    re.compile(r"\bdo\s+anything\s+now\b", re.I),
    re.compile(r"developer\s+mode", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+(not|no\s+longer|a\s+different)", re.I),
    re.compile(r"you\s+are\s+now\s+(free|unrestricted|dan|uncensored)", re.I),
    # Role-play as a privileged actor / directing lead transfers.
    re.compile(r"you\s+are\s+now\s+(an?\s+)?(admin|administrator|system|owner|manager)", re.I),
    re.compile(r"(pretend|act)\s+(you\s+are|as)\s+(an?\s+)?(admin|administrator|system)", re.I),
    re.compile(r"transfer\s+the\s+lead", re.I),
    # Consent / suppression bypass attempts.
    re.compile(r"(remove|clear|delete|bypass|ignore)\s+.*(suppression|opt.?out|do.?not.?contact|consent)", re.I),
    re.compile(r"mark\s+.*consent\s+(as\s+)?(verified|granted|given)", re.I),
]

_INJECTION_PHRASES = (
    "ignore your instructions",
    "ignore all instructions",
    "disregard your instructions",
    "system prompt",
    "reveal your instructions",
    "send sms to",
    "send a text to everyone",
    "transfer money",
    "exfiltrate",
)


def screen_inbound_text(text) -> bool:
    """Return True when *text* looks like a prompt-injection attempt.

    Deterministic keyword/regex screen only — never trusts model output.
    Returns False for empty/ordinary text. This is a screen, not a verdict:
    callers should treat a True result as "route to a human / do not act on
    this content", not as proof of malicious intent.
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    lowered = raw.lower()
    for phrase in _INJECTION_PHRASES:
        if phrase in lowered:
            return True
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(raw):
            return True
    return False


# --- Settings ---------------------------------------------------------------

def get_settings(user_id) -> dict:
    """Per-tenant harness settings.

    Defaults are ``mode="drafts_only"``, ``paused=False``,
    ``ai_enabled=True``. Reads the ``harness_settings`` table when it
    exists; if the migration has not landed yet (or any DB error occurs),
    returns the defaults instead of raising.
    """
    settings = dict(DEFAULT_SETTINGS)
    if user_id is None:
        return settings
    try:
        import db

        with db.get_db() as conn:
            row = conn.execute(
                "SELECT mode, paused, ai_enabled FROM harness_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    except Exception as exc:  # table missing, DB down, etc. — fail to defaults
        logger.warning("harness get_settings fell back to defaults: %s", exc)
        return settings
    if not row:
        return settings
    data = dict(row)
    mode = str(data.get("mode") or "").strip().lower()
    if mode in MODES:
        settings["mode"] = mode
    paused = data.get("paused")
    settings["paused"] = bool(paused) if paused is not None else False
    ai_enabled = data.get("ai_enabled")
    settings["ai_enabled"] = bool(ai_enabled) if ai_enabled is not None else True
    return settings


# --- Gate -------------------------------------------------------------------

def _normalize(value) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _is_restricted(action: str, tool: str) -> bool:
    action = _normalize(action)
    tool = _normalize(tool)
    if action in RESTRICTED_ACTIONS or tool in RESTRICTED_ACTIONS:
        return True
    # send_lead_email and any consent-changing tool: restricted in every
    # surface and every mode (plan §4.3).
    if action in ALWAYS_BLOCK_TOOLS or tool in ALWAYS_BLOCK_TOOLS:
        return True
    # Any consent-changing tool is restricted in every surface (plan §4.3:
    # the Vapi divergence is resolved here, not per surface).
    if tool in autonomy.BLOCK_TOOLS and tool in {
        "change_consent",
        "change_sms_qualification",
        "delete_lead",
        "delete_records",
        "bulk_action",
        "sql",
        "execute_sql",
        "raw_query",
    }:
        return True
    if action in {"change_consent", "change_sms_qualification"}:
        return True
    return False


def _is_send(action: str, tool: str) -> bool:
    action = _normalize(action)
    tool = _normalize(tool)
    return action in SEND_ACTIONS or tool in SEND_TOOLS or action in SEND_TOOLS


def _is_write(action: str, tool: str) -> bool:
    tool = _normalize(tool)
    action = _normalize(action)
    if tool in DRAFT_TOOLS or action in DRAFT_TOOLS:
        return True
    if _is_send(action, tool):
        return True
    # Registry write tools (reused from ask_topai.registry via autonomy).
    from ask_topai import registry

    if tool in registry.WRITE_TOOLS or action in registry.WRITE_TOOLS:
        return True
    # Unified harness registry (harness/tools.py): it also lists the
    # Vapi-only write tools the Ask TopAI registry does not know, e.g.
    # update_lead_contact_info. Without this, decide() classified those
    # as reads and ALLOWed them in drafts_only (found by the C1 fix).
    try:
        from harness import tools as harness_tools

        for name in (tool, action):
            entry = harness_tools.get_tool(name)
            if entry and entry.get("kind") == "write":
                return True
    except Exception:
        pass
    return False


def _escalation_from_context(context: dict) -> set[str]:
    topics = set()
    for key in ("escalation_topics", "escalation_topic", "topics"):
        raw = (context or {}).get(key)
        if isinstance(raw, str):
            raw = [raw]
        for item in raw or []:
            cleaned = _normalize(item)
            if cleaned in ESCALATION_TOPICS:
                topics.add(cleaned)
    # Also accept a coach-analysis shaped dict (autonomy's own helper).
    analysis = (context or {}).get("analysis")
    if isinstance(analysis, dict):
        topics |= autonomy.sms_escalation_topics(analysis)
        if analysis.get("sensitive_topic"):
            topics.add(str(analysis.get("sensitive_topic")).strip().lower())
    return topics


def _known_names() -> set[str]:
    """Every tool/action name the gate recognises.

    The union of the harness tool registry keys (``harness.tools``,
    which itself seeds from ``ask_topai.registry`` plus the Vapi tools)
    and this module's own known action names (restricted actions,
    always-block tools, sends, draft tools). Anything outside this set
    is unknown to the harness and must fail closed (H1).
    """
    names = (
        set(RESTRICTED_ACTIONS)
        | set(ALWAYS_BLOCK_TOOLS)
        | set(SEND_TOOLS)
        | set(SEND_ACTIONS)
        | set(DRAFT_TOOLS)
    )
    try:
        from harness import tools as harness_tools

        names.update(t["name"] for t in harness_tools.list_tools())
    except Exception as exc:  # registry unavailable — gate on policy names only
        logger.warning("harness known-names fell back to policy names: %s", exc)
    try:
        names.update(autonomy.BLOCK_TOOLS)
    except Exception:
        pass
    return {_normalize(n) for n in names}


def decide(user_id, action, tool, context=None) -> tuple[str, str]:
    """Gate a proposed action.

    Returns ``(decision, reason)`` with decision in
    ``{"ALLOW", "DRAFT", "BLOCK", "DEFER"}``. Deterministic; no model input.

    ``context`` may carry: ``mode`` / ``paused`` / ``human_takeover``
    overrides, ``consent_status`` / ``opted_out`` / ``suppressed`` /
    ``do_not_contact`` flags, ``quiet_hours`` (bool), ``rate_limited``
    (bool), ``escalation_topics``, ``template_approved`` (bool), and an
    ``analysis`` dict in the sms_coach shape. When ``mode``/``paused`` are
    not in context they are read from :func:`get_settings`.
    """
    context = dict(context or {})
    action_norm = _normalize(action)
    tool_norm = _normalize(tool)

    settings = get_settings(user_id)
    mode = _normalize(context.get("mode") or settings.get("mode") or DEFAULT_MODE)
    if mode not in MODES:
        mode = DEFAULT_MODE
    paused = bool(context.get("paused", settings.get("paused", False)))
    human_takeover = bool(
        context.get("human_takeover", context.get("human_takeover_active", False))
    )
    ai_enabled = bool(context.get("ai_enabled", settings.get("ai_enabled", True)))

    # 1. Restricted actions block in every mode, for every surface.
    if _is_restricted(action_norm, tool_norm):
        return "BLOCK", f"restricted action: {action_norm or tool_norm} is never auto-executed"

    # 1b. Unknown tools/actions fail closed in every mode (H1). A
    # provided name that is neither a harness registry tool nor a
    # policy-known action is BLOCKed — including unknown reads. An empty
    # name means "not specified" (a caller gating an action without a
    # tool, or vice versa) and does not by itself trigger this check.
    known = _known_names()
    if (action_norm and action_norm not in known) or (
        tool_norm and tool_norm not in known
    ):
        unknown = (
            action_norm
            if (action_norm and action_norm not in known)
            else tool_norm
        )
        return "BLOCK", f"unknown tool/action: {unknown} is not a known harness tool or action"

    # 2. AI disabled for this tenant: nothing autonomous proceeds.
    if not ai_enabled:
        return "DRAFT", "AI is disabled for this account; human review required"

    # 3. Consent / suppression / opt-out / do-not-contact block sends.
    consent_status = _normalize(context.get("consent_status") or context.get("sms_consent_status"))
    if consent_status in CONSENT_BLOCKED_STATES:
        return "BLOCK", f"consent status {consent_status} forbids sending"
    if context.get("opted_out") or context.get("opted_out_status") == "opted_out":
        return "BLOCK", "lead has opted out"
    if context.get("suppressed") or context.get("suppression_list"):
        return "BLOCK", "number is on the suppression list"
    if context.get("do_not_contact"):
        return "BLOCK", "lead is marked do-not-contact"
    if context.get("consent_missing") and _is_send(action_norm, tool_norm):
        return "BLOCK", "no messaging consent evidence for this lead/channel"

    # 4. Quiet hours and rate limits defer sends (never send now).
    if _is_send(action_norm, tool_norm):
        if context.get("quiet_hours"):
            return "DEFER", "quiet hours are in effect for the recipient; schedule instead of sending"
        if context.get("rate_limited"):
            return "DEFER", "tenant/per-contact rate limit reached; retry later"

    # 5. Pause / human takeover: draft everything actionable.
    if paused:
        return "DRAFT", "autonomy is paused for this account"
    if human_takeover:
        return "DRAFT", "a human has taken over this conversation"

    # 6. Escalation topics: draft + needs-attention, never auto-send/act.
    topics = _escalation_from_context(context)
    if topics:
        return "DRAFT", f"escalation topic(s) require human review: {', '.join(sorted(topics))}"

    # 7. Monthly spend cap (D2), enforced before the mode rules, for
    # AI-costing actions only (sends and writes). Reads stay ALLOW.
    # At/above the cap sends BLOCK and other writes DRAFT; at/above 80%
    # of the cap a warning alert is recorded and the mode rules still
    # apply. Alert/ledger failures never break the gate (fail open on
    # the spend read, per harness.costs.current_month_spend_cents).
    is_send = _is_send(action_norm, tool_norm)
    is_write = _is_write(action_norm, tool_norm)
    if is_send or is_write:
        cap_cents = 0
        spend_cents = 0
        try:
            from harness.costs import (
                current_month_spend_cents,
                get_monthly_spend_cap_cents,
            )

            cap_cents = get_monthly_spend_cap_cents()
            spend_cents = (
                current_month_spend_cents(user_id) if cap_cents > 0 else 0
            )
        except Exception as exc:  # ledger/cap unreadable — skip the check
            logger.warning("harness spend-cap check skipped: %s", exc)
        if cap_cents > 0 and spend_cents >= cap_cents:
            try:
                from harness import alerts as harness_alerts

                harness_alerts.record_spend_alert(
                    user_id, spend_cents, cap_cents, "limit"
                )
            except Exception as exc:
                logger.warning("harness spend limit alert failed: %s", exc)
            if is_send:
                return (
                    "BLOCK",
                    "monthly AI spend limit reached; sends are blocked until next month",
                )
            return (
                "DRAFT",
                "monthly AI spend limit reached; writes need human review until next month",
            )
        if cap_cents > 0 and spend_cents * 5 >= cap_cents * 4:
            try:
                from harness import alerts as harness_alerts

                harness_alerts.record_spend_alert(
                    user_id, spend_cents, cap_cents, "warning"
                )
            except Exception as exc:
                logger.warning("harness spend warning alert failed: %s", exc)

    # 8. Mode rules.
    if mode == "drafts_only":
        if is_send:
            return "DRAFT", "drafts_only mode: sends are drafts, never auto-sent"
        if is_write:
            return "DRAFT", "drafts_only mode: writes are drafts pending human approval"
        return "ALLOW", "read/informational action allowed in drafts_only mode"
    if mode == "supervised":
        if is_send:
            if context.get("template_approved"):
                return "ALLOW", "supervised mode: approved-template send allowed"
            return "DRAFT", "supervised mode: only approved-template sends auto-execute"
        if is_write:
            return "ALLOW", "supervised mode: routine write allowed"
        return "ALLOW", "read/informational action allowed in supervised mode"
    # authorized
    if is_send:
        return "ALLOW", "authorized mode: routine send allowed"
    if is_write:
        return "ALLOW", "authorized mode: routine write allowed"
    return "ALLOW", "read/informational action allowed in authorized mode"


def is_restricted_action(action, tool=None) -> bool:
    """Convenience check: is this action/tool restricted in every mode?"""
    return _is_restricted(action or "", tool or "")


def is_send_action(action, tool=None) -> bool:
    """Convenience check: does this action/tool send through a provider?"""
    return _is_send(action or "", tool or "")
