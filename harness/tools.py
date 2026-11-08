"""Unified harness tool registry.

Seeds from the existing registries — :mod:`ask_topai.registry`
(``WRITE_TOOLS`` / ``READ_TOOLS`` / ``CONTROL_TOOLS`` / ``FUTURE_TOOLS``)
plus the 6 Vapi server tools in :mod:`voice_tools` — and adds one risk
class, surface list, idempotency-key derivation, and cost weight per tool.

Risk classes:

* ``routine`` — safe to auto-execute within the current mode.
* ``sensitive`` — mutates records or drafts outbound content; drafts in
  Drafts Only, allowed as routine writes in Supervised/Authorized.
  Generic sends (``send_sms`` / ``send_email`` / ``initiate_ai_call`` /
  ``place_call`` / ``start_call`` / ``send_listings``) are ``sensitive``,
  not ``restricted``: they are mode-gated, so authorized autonomy may
  allow them — that is the point of Mode 3, and
  ``harness.policy.decide`` already allows them there.
* ``restricted`` — never auto-executes in any surface. This resolves the
  per-surface guardrail divergence (plan §1.7): ``send_lead_email`` (a
  real SendGrid send) and any consent-changing tool are ``restricted``
  in **all** surfaces, not just Ask TopAI.

Registry/policy seam (H2 reconciliation — single source of truth is
this risk class together with ``harness.policy``): ``restricted`` here
⟺ ``policy.decide`` BLOCKs in all three modes; ``routine`` /
``sensitive`` ⟺ ``decide`` does NOT BLOCK in all modes (the mode rules
decide whether it is ALLOW or DRAFT). The registry used to mark the
generic sends ``restricted`` while the policy allowed them in
authorized mode; the registry was wrong, not the policy, and was
corrected here. ``tests/test_harness_policy_gate.py`` asserts the
agreement for every tool in this registry.

No tool here executes anything. This module is metadata only; execution
stays with the existing handlers (``ask_topai/tools.py``,
``voice_tools.py``, providers).
"""

from __future__ import annotations

from ask_topai import registry as _ask_registry

# The 6 Vapi server tools, named exactly as in voice_tools.py.
VAPI_TOOLS = (
    "list_open_leads",
    "update_lead_status",
    "update_lead_sms_consent_status",
    "update_lead_contact_info",
    "draft_lead_email",
    "send_lead_email",
)

# Tools that are blocked outright in autonomy.py but have no Ask TopAI
# registry entry. They are registered here as restricted metadata so
# is_restricted() is complete for every surface. The D3 subscription /
# customer actions are registered here too, so the registry/policy
# agreement check covers them as well (policy.RESTRICTED_ACTIONS is
# the enforcement side; this is the metadata side).
BLOCKED_ONLY_TOOLS = (
    "delete_lead",
    "delete_records",
    "bulk_action",
    "change_consent",
    "change_sms_qualification",
    "sql",
    "execute_sql",
    "raw_query",
    "send_listings",
    "place_call",
    "start_call",
    "change_subscription",
    "modify_subscription",
    "update_subscription",
    "change_plan",
    "update_plan",
    "delete_customer",
    "cancel_subscription",
    "cancel_plan",
)

# Every send tool, across surfaces.
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

# Idempotency-key derivation descriptions (how a caller must derive the key;
# the registry itself stores no keys).
_IDEMPOTENCY_READ = "none — read-only, no idempotency key needed"
_IDEMPOTENCY_LEAD_WRITE = (
    "sha256(user_id + tool + lead_id + canonical_json(arguments)) — "
    "one key per distinct lead mutation"
)
_IDEMPOTENCY_CREATE = (
    "sha256(user_id + tool + normalized_phone_or_email + canonical_json(arguments)) — "
    "dedupes double-submit creates for the same contact"
)
_IDEMPOTENCY_CALENDAR = (
    "sha256(user_id + tool + lead_id + start_at + end_at) — "
    "one key per distinct appointment slot"
)
_IDEMPOTENCY_SEND = (
    "sha256(user_id + tool + lead_id + channel + message_hash + purpose) — "
    "one key per distinct outbound message; reusing the key must not re-send"
)
_IDEMPOTENCY_DRAFT = (
    "sha256(user_id + tool + lead_id + subject_or_first_line + canonical_json(arguments)) — "
    "drafts are idempotent by content hash"
)
_IDEMPOTENCY_CONTROL = "none — conversational control, no side effect"
_IDEMPOTENCY_CONSENT = (
    "sha256(user_id + tool + lead_id + new_consent_status) — "
    "consent changes require an approval row, not just a key"
)


def _entry(name, surfaces, kind, risk, idempotency, cost_weight):
    return {
        "name": name,
        "surfaces": tuple(surfaces),
        "kind": kind,  # "read" | "write" | "control"
        "risk_class": risk,  # "routine" | "sensitive" | "restricted"
        "idempotency_key": idempotency,
        "cost_weight": cost_weight,
    }


def _build_registry() -> dict:
    tools = {}

    for name in sorted(_ask_registry.READ_TOOLS):
        tools[name] = _entry(
            name, ("claude",), "read", "routine", _IDEMPOTENCY_READ, 1
        )
    for name in sorted(_ask_registry.WRITE_TOOLS):
        tools[name] = _entry(
            name,
            ("claude",),
            "write",
            "routine",
            _IDEMPOTENCY_CALENDAR if "calendar" in name else _IDEMPOTENCY_LEAD_WRITE,
            2,
        )
    # create_lead gets a create-specific idempotency derivation.
    tools["create_lead"] = _entry(
        "create_lead", ("claude",), "write", "sensitive", _IDEMPOTENCY_CREATE, 2
    )
    for name in sorted(_ask_registry.CONTROL_TOOLS):
        tools[name] = _entry(
            name, ("claude",), "control", "routine", _IDEMPOTENCY_CONTROL, 0
        )

    # Future tools: declared in ask_topai.registry but not sent to any model.
    # They are registered here so the gate can classify them before they are
    # ever enabled; generic sends are mode-gated ("sensitive", H2 — the
    # policy allows them in authorized mode), drafts are sensitive. Only
    # send_lead_email stays "restricted"; it is not a future tool, but
    # guard it here anyway so a future registry addition cannot
    # silently reclassify a real SendGrid send as mode-gated.
    for name in _ask_registry.FUTURE_TOOLS:
        if name in tools:
            continue
        if name in SEND_TOOLS:
            risk = "restricted" if name == "send_lead_email" else "sensitive"
            tools[name] = _entry(
                name, ("claude", "sms", "email"), "write", risk,
                _IDEMPOTENCY_SEND, 5,
            )
        elif name.startswith("draft_"):
            tools[name] = _entry(
                name, ("claude", "sms", "email"), "write", "sensitive",
                _IDEMPOTENCY_DRAFT, 2,
            )
        else:
            tools[name] = _entry(
                name, ("claude",), "read", "routine", _IDEMPOTENCY_READ, 2
            )

    # Vapi tools. Names are taken verbatim from voice_tools.py.
    # list_open_leads / update_lead_status already exist from the Claude
    # registry — merge the "vapi" surface onto them.
    for name in VAPI_TOOLS:
        if name == "list_open_leads":
            entry = tools[name]
            entry["surfaces"] = tuple(sorted(set(entry["surfaces"]) | {"vapi"}))
            continue
        if name == "update_lead_status":
            # Vapi accepts any status incl. closed_won (unlike Ask TopAI),
            # so the merged entry is sensitive, not routine.
            entry = tools[name]
            entry["surfaces"] = tuple(sorted(set(entry["surfaces"]) | {"vapi"}))
            entry["risk_class"] = "sensitive"
            continue
        if name == "update_lead_sms_consent_status":
            # Consent-changing tool: restricted in ALL surfaces.
            tools[name] = _entry(
                name, ("vapi", "claude"), "write", "restricted",
                _IDEMPOTENCY_CONSENT, 3,
            )
            continue
        if name == "update_lead_contact_info":
            tools[name] = _entry(
                name, ("vapi",), "write", "sensitive",
                _IDEMPOTENCY_LEAD_WRITE, 2,
            )
            continue
        if name == "draft_lead_email":
            tools[name] = _entry(
                name, ("vapi", "email"), "write", "sensitive",
                _IDEMPOTENCY_DRAFT, 2,
            )
            continue
        if name == "send_lead_email":
            # Real send via SendGrid: restricted in ALL surfaces.
            tools[name] = _entry(
                name, ("vapi", "email"), "write", "restricted",
                _IDEMPOTENCY_SEND, 5,
            )
            continue

    # Blocked-only tools (autonomy.BLOCK_TOOLS with no registry entry).
    # Generic sends are mode-gated ("sensitive", H2); every other name
    # here stays "restricted". send_lead_email is the one send that
    # stays restricted in all surfaces.
    for name in BLOCKED_ONLY_TOOLS:
        risk = (
            "sensitive"
            if (name in SEND_TOOLS and name != "send_lead_email")
            else "restricted"
        )
        if name in tools:
            tools[name]["risk_class"] = risk
            continue
        tools[name] = _entry(
            name, ("claude",), "write", risk,
            _IDEMPOTENCY_SEND if name in SEND_TOOLS else _IDEMPOTENCY_CONSENT,
            5,
        )

    return tools


_REGISTRY = _build_registry()


def get_tool(name):
    """Return the registry entry for *name*, or None if unknown."""
    if not name:
        return None
    return _REGISTRY.get(str(name).strip())


def list_tools(surface=None, kind=None, risk=None):
    """All registry entries, optionally filtered by surface/kind/risk."""
    items = list(_REGISTRY.values())
    if surface:
        items = [t for t in items if surface in t["surfaces"]]
    if kind:
        items = [t for t in items if t["kind"] == kind]
    if risk:
        items = [t for t in items if t["risk_class"] == risk]
    return sorted(items, key=lambda t: t["name"])


def risk_class(name) -> str:
    """Risk class of *name*; unknown tools fail closed as ``restricted``."""
    entry = get_tool(name)
    if entry is None:
        return "restricted"
    return entry["risk_class"]


def is_restricted(name) -> bool:
    """True when the tool must never auto-execute in any surface."""
    return risk_class(name) == "restricted"


def is_send_tool(name) -> bool:
    """True when the tool sends a message/call through a provider."""
    if not name:
        return False
    return str(name).strip() in SEND_TOOLS


def is_read_tool(name) -> bool:
    entry = get_tool(name)
    return entry is not None and entry["kind"] == "read"


def is_write_tool(name) -> bool:
    entry = get_tool(name)
    return entry is not None and entry["kind"] == "write"
