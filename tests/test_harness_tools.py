"""L1: idempotency-key derivations for blocked-only tools.

The acceptance report found blocked-only tools carrying wrong
derivations (e.g. delete_lead / execute_sql described as consent
changes). Every blocked-only tool that never executes must say so;
the mode-gated sends among them keep the per-message send derivation,
and the real consent tool keeps its consent derivation.
"""

from harness import tools


def test_blocked_only_non_send_tools_have_no_key_derivation():
    """delete / SQL / subscription / consent-change tools never execute,
    so no idempotency key is derived for them — and the derivation must
    not describe a consent change, a send, or any other action."""
    never_execute = {
        "delete_lead",
        "delete_records",
        "bulk_action",
        "change_consent",
        "change_sms_qualification",
        "sql",
        "execute_sql",
        "raw_query",
        "change_subscription",
        "modify_subscription",
        "update_subscription",
        "change_plan",
        "update_plan",
        "delete_customer",
        "cancel_subscription",
        "cancel_plan",
    }
    for name in never_execute:
        entry = tools.get_tool(name)
        assert entry is not None, f"{name} missing from registry"
        assert entry["risk_class"] == "restricted", name
        derivation = entry["idempotency_key"]
        assert "never executes" in derivation, f"{name}: {derivation}"
        assert "consent" not in derivation.lower(), f"{name}: {derivation}"
        assert "outbound message" not in derivation, f"{name}: {derivation}"


def test_blocked_only_sends_keep_send_derivation():
    """The sends in BLOCKED_ONLY_TOOLS are mode-gated (sensitive), not
    blocked: they execute in authorized mode, so a per-message key
    derivation is meaningful for them."""
    for name in ("send_listings", "place_call", "start_call"):
        entry = tools.get_tool(name)
        assert entry is not None, f"{name} missing from registry"
        assert entry["risk_class"] == "sensitive", name
        derivation = entry["idempotency_key"]
        assert "outbound message" in derivation, f"{name}: {derivation}"
        assert "never executes" not in derivation, f"{name}: {derivation}"


def test_real_consent_tool_keeps_consent_derivation():
    """update_lead_sms_consent_status is a real (restricted) tool with
    approval-row semantics — its consent derivation is correct and must
    not have been swept into the blocked derivation."""
    entry = tools.get_tool("update_lead_sms_consent_status")
    assert entry is not None
    assert entry["risk_class"] == "restricted"
    assert "new_consent_status" in entry["idempotency_key"]
    assert "approval row" in entry["idempotency_key"]


def test_every_registry_tool_has_a_derivation():
    """No tool ships without an idempotency statement."""
    for entry in tools.list_tools():
        assert entry["idempotency_key"], entry["name"]
        assert isinstance(entry["idempotency_key"], str)
