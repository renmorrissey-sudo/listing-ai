"""Policy-gate deficiency tests (H1, H2, D3, D2).

Covers the fixes from the AI Harness Phase I acceptance test:

* H1 — unknown tools/actions fail closed (BLOCK in every mode).
* H2 — the harness tool registry and ``policy.decide`` agree on every
  tool: ``restricted`` ⟺ BLOCK in all three modes.
* D3 — subscription-change and customer-delete actions are restricted.
* D2 — a per-tenant monthly spend cap is enforced in the gate, with
  spend alerts written to the audit log.

Deterministic only: no model, no provider, no network. Spend is
recorded directly through :mod:`harness.costs` against the temp
SQLite DB (``tests/conftest.py``).
"""

import pytest

from harness import alerts as harness_alerts
from harness import costs
from harness import policy
from harness import tools as harness_tools
from harness.policy import MODES, decide


def _record_spend(user_id, cost_cents, units=100):
    return costs.record_cost(
        user_id, "anthropic", "send_sms", units, "tokens",
        cost_cents=cost_cents,
    )


def _spend_alerts(user_id):
    from harness.audit import list_audit

    return [
        row for row in list_audit(user_id)
        if row["action"] == "spend_alert"
    ]


# --- H1: unknown tools fail closed -----------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_unknown_tool_blocks_in_all_modes(two_users, mode):
    u1, _ = two_users
    decision, reason = decide(
        u1, "frobnicate_tool_xyz", "frobnicate_tool_xyz", {"mode": mode}
    )
    assert decision == "BLOCK", f"frobnicate_tool_xyz in {mode}: {reason}"
    assert "unknown" in reason.lower()


@pytest.mark.parametrize("mode", MODES)
def test_unknown_read_like_name_blocks_in_all_modes(two_users, mode):
    u1, _ = two_users
    # Reads fail closed too: an unknown name is never an allowed read.
    decision, reason = decide(
        u1, "fetch_quantum_listings", "fetch_quantum_listings",
        {"mode": mode},
    )
    assert decision == "BLOCK", f"unknown read in {mode}: {reason}"


def test_unknown_action_blocks_even_with_known_tool(two_users):
    u1, _ = two_users
    decision, _ = decide(
        u1, "frobnicate_tool_xyz", "send_sms", {"mode": "authorized"}
    )
    assert decision == "BLOCK"


def test_unknown_tool_blocks_even_with_known_action(two_users):
    u1, _ = two_users
    decision, _ = decide(
        u1, "send_sms", "frobnicate_tool_xyz", {"mode": "authorized"}
    )
    assert decision == "BLOCK"


def test_known_tools_still_gate_normally_after_h1(two_users):
    u1, _ = two_users
    assert decide(u1, "find_lead", "find_lead", {"mode": "authorized"})[0] == "ALLOW"
    assert decide(u1, "send_sms", "send_sms", {"mode": "authorized"})[0] == "ALLOW"
    assert decide(u1, "send_sms", "send_sms", {"mode": "drafts_only"})[0] == "DRAFT"


# --- H2: registry / policy agreement for every tool -------------------------


def test_registry_policy_agreement_for_every_tool(two_users):
    u1, _ = two_users
    entries = harness_tools.list_tools()
    assert entries, "registry must not be empty"
    for entry in entries:
        name = entry["name"]
        risk = entry["risk_class"]
        decisions = {
            mode: decide(u1, name, name, {"mode": mode})[0]
            for mode in MODES
        }
        if risk == "restricted":
            assert all(d == "BLOCK" for d in decisions.values()), (
                f"restricted tool {name} must BLOCK in all modes, got {decisions}"
            )
        else:
            assert risk in ("routine", "sensitive"), f"{name}: bad risk {risk!r}"
            assert not all(d == "BLOCK" for d in decisions.values()), (
                f"{risk} tool {name} must NOT BLOCK in all modes, got {decisions}"
            )


def test_generic_sends_are_sensitive_not_restricted():
    # H2 reconciliation direction: the policy was right (mode-gated
    # sends), the registry was wrong. Generic sends are "sensitive";
    # only the real SendGrid send stays restricted.
    for name in (
        "send_sms", "send_email", "initiate_ai_call",
        "place_call", "start_call", "send_listings",
    ):
        assert harness_tools.risk_class(name) == "sensitive", name
        assert harness_tools.is_restricted(name) is False, name
    assert harness_tools.risk_class("send_lead_email") == "restricted"
    assert harness_tools.is_restricted("send_lead_email") is True


def test_send_lead_email_blocks_in_all_modes(two_users):
    u1, _ = two_users
    for mode in MODES:
        decision, _ = decide(u1, "send_lead_email", "send_lead_email", {"mode": mode})
        assert decision == "BLOCK"


# --- D3: subscription-change / customer-delete restricted -------------------


@pytest.mark.parametrize(
    "name",
    [
        "change_subscription",
        "modify_subscription",
        "update_subscription",
        "delete_customer",
        "change_plan",
        "update_plan",
        "cancel_subscription",
    ],
)
@pytest.mark.parametrize("mode", MODES)
def test_subscription_customer_actions_block_in_all_modes(two_users, name, mode):
    u1, _ = two_users
    decision, reason = decide(u1, name, name, {"mode": mode})
    assert decision == "BLOCK", f"{name} in {mode}: {reason}"
    assert harness_tools.is_restricted(name) is True, name


# --- D2: monthly spend cap ---------------------------------------------------


def test_current_month_spend_sums_cost_cents(two_users):
    u1, u2 = two_users
    assert costs.current_month_spend_cents(u1) == 0
    _record_spend(u1, 250)
    _record_spend(u1, 50)
    assert costs.current_month_spend_cents(u1) == 300
    assert costs.current_month_spend_cents(u2) == 0
    assert costs.current_month_spend_cents(None) == 0


def test_spend_cap_default_and_env_override(monkeypatch):
    monkeypatch.delenv("HARNESS_MONTHLY_SPEND_CAP_CENTS", raising=False)
    assert costs.get_monthly_spend_cap_cents() == 5000
    monkeypatch.setenv("HARNESS_MONTHLY_SPEND_CAP_CENTS", "100")
    assert costs.get_monthly_spend_cap_cents() == 100
    monkeypatch.setenv("HARNESS_MONTHLY_SPEND_CAP_CENTS", "not-a-number")
    assert costs.get_monthly_spend_cap_cents() == 5000


def test_spend_below_cap_send_allowed_in_authorized(two_users):
    u1, _ = two_users
    _record_spend(u1, 100)
    decision, _ = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "ALLOW"
    assert _spend_alerts(u1) == []


def test_spend_at_or_above_cap_blocks_send_in_authorized(two_users):
    u1, _ = two_users
    # The acceptance-test example: 10,000,000 tokens / $50,000 of usage.
    _record_spend(u1, 5_000_000, units=10_000_000)
    decision, reason = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "BLOCK", reason
    assert "spend" in reason.lower() and "limit" in reason.lower()
    alerts = _spend_alerts(u1)
    assert len(alerts) == 1
    assert alerts[0]["gate_decision"] == "BLOCK"
    assert "level=limit" in alerts[0]["gate_reason"]


def test_spend_over_cap_drafts_writes_but_allows_reads(two_users):
    u1, _ = two_users
    _record_spend(u1, 5_000_000, units=10_000_000)
    decision, reason = decide(
        u1, "update_lead_status", "update_lead_status", {"mode": "authorized"}
    )
    assert decision == "DRAFT", reason
    decision, _ = decide(u1, "find_lead", "find_lead", {"mode": "authorized"})
    assert decision == "ALLOW"


def test_spend_at_80_percent_warns_but_allows_send(two_users):
    u1, _ = two_users
    _record_spend(u1, 4000)  # exactly 80% of the 5000-cent default cap
    decision, _ = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "ALLOW"
    alerts = _spend_alerts(u1)
    assert len(alerts) == 1
    assert alerts[0]["gate_decision"] == "DRAFT"
    assert "level=warning" in alerts[0]["gate_reason"]
    assert "spend_cents=4000" in alerts[0]["gate_reason"]


def test_spend_below_80_percent_produces_no_alert(two_users):
    u1, _ = two_users
    _record_spend(u1, 3999)
    decision, _ = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "ALLOW"
    assert _spend_alerts(u1) == []


def test_cap_configurable_via_env_var(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setenv("HARNESS_MONTHLY_SPEND_CAP_CENTS", "100")
    _record_spend(u1, 50)
    decision, _ = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "ALLOW"
    _record_spend(u1, 60)  # month-to-date is now 110 cents, over the cap
    decision, reason = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "BLOCK", reason
    assert _spend_alerts(u1), "a limit alert must be recorded"


def test_spend_alert_dedupes_within_month(two_users):
    u1, _ = two_users
    _record_spend(u1, 5_000_000, units=10_000_000)
    decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert len(_spend_alerts(u1)) == 1


def test_record_spend_alert_rejects_unknown_level(two_users):
    u1, _ = two_users
    assert harness_alerts.record_spend_alert(u1, 100, 5000, "bogus") is None
    assert _spend_alerts(u1) == []
    assert harness_alerts.record_spend_alert(None, 100, 5000, "warning") is None


def test_record_spend_alert_writes_audit_row_and_returns_id(two_users):
    u1, _ = two_users
    audit_id = harness_alerts.record_spend_alert(u1, 4500, 5000, "warning")
    assert audit_id is not None
    alerts = _spend_alerts(u1)
    assert len(alerts) == 1
    assert alerts[0]["id"] == audit_id
    assert alerts[0]["actor"] == "system"
    assert alerts[0]["gate_decision"] == "DRAFT"


def test_vapi_only_write_tool_is_a_write_in_drafts_only(two_users):
    """Integration fix (C1 follow-up): policy._is_write must classify
    Vapi-only write tools via the unified harness registry, not just the
    Ask TopAI registry. update_lead_contact_info exists only in the Vapi
    surface; before this fix decide() returned ALLOW for it in
    drafts_only, and only the voice dispatch workaround saved it.
    """
    u1, _ = two_users
    decision, _reason = decide(
        u1, "update_lead_contact_info", "update_lead_contact_info"
    )
    assert decision == "DRAFT", _reason
    decision, _reason = decide(u1, "list_open_leads", "list_open_leads")
    assert decision == "ALLOW", _reason
