"""Approval execution + escalation handling (defect 1 remediation).

Regression tests for the November 9 live staging blocker: a routine
pricing/availability inquiry could not execute after human approval
because the send gate declined it with ``escalation_topic`` and the
worker treated that deterministic decline as retryable, retrying
forever without sending.

The fix:
* A human approval overrides ROUTINE escalation topics
  (uncertain_property_fact, negotiation, financing) — the human has
  reviewed the draft that answers them. Hard topics (legal,
  fair_housing, complaint) still block even an approved send.
* A deterministic send decline settles the approval as ``blocked``
  (never sent, never retried); only transient failures revert to
  approved for a worker retry.
* The approval surfaces the escalation topics + gate reason recorded
  at draft time, so the human sees them BEFORE approving.
* The coach prompt flags ``uncertain_property_fact`` only when the
  draft asserts an unverifiable property fact — a deferral that only
  promises agent follow-up is not an escalation.

Deterministic only: no model, no provider, no network.
"""

from unittest.mock import patch

import pytest

import config
from tests.test_harness_staging_safety import (
    _GateOpen,
    _approval_row,
    _set_settings,
    _staging_analysis,
    _staging_approval,
    _staging_lead,
    _staging_mock_config,
    staging,  # noqa: F401  (fixture, re-exported for pytest)
)


def _analysis_with_topics(topics, *, sensitive=True, draft=None):
    """A coach analysis for a pricing/availability inquiry."""
    analysis = _staging_analysis()
    analysis["escalation_topics"] = list(topics)
    analysis["sensitive_topic"] = sensitive
    analysis["requires_manual_review"] = sensitive
    if draft is not None:
        analysis["draft_reply"] = draft
        analysis["suggested_reply"] = draft
    return analysis


def _approved_execution(u1, approval_id, analysis, monkeypatch):
    """Run execute_approved.

    The coach is NOT mocked to return `analysis` here: with
    approved-draft fidelity the coach is skipped for approved sends
    and the STORED draft (escalation topics + text) governs. The
    `analysis` argument is kept for the callers that still build one,
    but it is ignored — the stored draft is what executes. To prove
    the coach is not re-run, the coach is patched to RAISE if called.
    """
    from harness.approvals import execute_approved

    def _coach_must_not_run(*args, **kwargs):
        raise AssertionError(
            "coach re-run during approved execution (fidelity violation)"
        )

    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        side_effect=_coach_must_not_run,
    ):
        return execute_approved(u1, approval_id)


# Distinct inbound ids per test: the AI-reply slot has a UNIQUE index
# on reply_to_message_id, so reusing the default 424242 across tests
# that actually send would trip "already_replied".
_INBOUND_PRICING = 424210
_INBOUND_NEGOTIATION = 424211
_INBOUND_LEGAL = 424212
_INBOUND_COMPLAINT = 424213
_INBOUND_AUTO = 424214


def test_approved_execution_sends_routine_pricing_inquiry(
    staging, two_users, monkeypatch
):
    """The live blocker, reproduced: a pricing question flagged
    uncertain_property_fact must execute after human approval — exactly
    one mock send, approval executed. Pre-fix this declined with
    escalation_topic and the approval reverted to approved, retrying
    forever."""
    from harness.approvals import approve_approval, execute_approved
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_phone = "+13035550210"
    lead_id = _staging_lead(u1, lead_phone)
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=_INBOUND_PRICING,
        draft_extra={
            "escalation_topics": ["uncertain_property_fact"],
            "draft_reply": "Hi! Thanks for reaching out — let me have our agent verify "
            "the latest availability and pricing details for you.",
        },
    )
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()
    analysis = _analysis_with_topics(
        ["uncertain_property_fact"],
        draft="Hi! Thanks for reaching out — let me have our agent verify "
        "the latest availability and pricing details for you.",
    )
    result = _approved_execution(u1, approval_id, analysis, monkeypatch)
    assert result["sent"] is True, result
    assert result["decision"] == "ALLOW"
    assert result["retryable"] is False
    assert _approval_row(approval_id)["status"] == "executed"
    recorded = sent_messages()
    assert len(recorded) == 1
    assert recorded[0]["to"] == lead_phone
    assert recorded[0]["status"] == "mock_sent"


def test_approved_execution_sends_routine_negotiation_topic(
    staging, two_users, monkeypatch
):
    """Routine negotiation/financing topics are likewise overridden by
    a human approval."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550211")
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=_INBOUND_NEGOTIATION,
        draft_extra={"escalation_topics": ["negotiation", "financing"]},
    )
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()
    analysis = _analysis_with_topics(["negotiation", "financing"])
    result = _approved_execution(u1, approval_id, analysis, monkeypatch)
    assert result["sent"] is True, result
    assert _approval_row(approval_id)["status"] == "executed"
    assert len(sent_messages()) == 1


def test_approved_execution_still_blocks_hard_escalation(
    staging, two_users, monkeypatch
):
    """Hard escalation topics (legal, fair housing, complaint) still
    block even an approved send — approval must not override them."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550212")
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=_INBOUND_LEGAL,
        draft_extra={"escalation_topics": ["legal"]},
    )
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()
    analysis = _analysis_with_topics(["legal"])
    result = _approved_execution(u1, approval_id, analysis, monkeypatch)
    assert result["sent"] is False, result
    assert result["reason"] == "escalation_topic"
    assert sent_messages() == []


def test_deterministic_decline_settles_blocked_not_retryable(
    staging, two_users, monkeypatch
):
    """A deterministic decline (here: hard escalation topic) settles
    the approval as blocked and is NOT retryable — it must not revert
    to approved and retry forever."""
    from harness.approvals import approve_approval

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550213")
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=_INBOUND_COMPLAINT,
        draft_extra={"escalation_topics": ["complaint"]},
    )
    assert approve_approval(u1, approval_id) is not None
    analysis = _analysis_with_topics(["complaint"])
    result = _approved_execution(u1, approval_id, analysis, monkeypatch)
    assert result["sent"] is False
    assert result["retryable"] is False, result
    assert result["decision"] == "BLOCK"
    assert _approval_row(approval_id)["status"] == "blocked"


def test_auto_send_path_still_blocks_escalation_topics(
    staging, two_users, monkeypatch
):
    """The auto-send path (no human approval) is unchanged: any
    escalation topic still blocks the send."""
    import sms_ai_agent

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550214")
    analysis = _analysis_with_topics(["uncertain_property_fact"])
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": analysis, "insight_id": None, "error": None},
    ):
        result = sms_ai_agent.process_inbound_ai(
            u1, lead_id, _INBOUND_AUTO, "Is it still available?", "+18888210810"
        )
    assert result["replied"] is False
    assert result["reason"] == "escalation_topic"


def test_approval_surfaces_escalation_topics_before_decision(
    staging, two_users, monkeypatch
):
    """serialize_approval exposes the escalation topics + gate reason
    recorded at draft time, so the human sees them BEFORE approving."""
    import json

    import db
    from harness.approvals import serialize_approval

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    lead_id = _staging_lead(u1, "+13035550215")
    approval_id = _staging_approval(u1, lead_id)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_approvals SET draft_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "draft_reply": "Hi! Let me verify that for you.",
                        "lead_id": lead_id,
                        "inbound_id": 424242,
                        "escalation_topics": ["uncertain_property_fact"],
                        "gate_reason": "escalation_topic",
                    }
                ),
                approval_id,
            ),
        )
    row = _approval_row(approval_id)
    serialized = serialize_approval(row)
    assert serialized["escalation_topics"] == ["uncertain_property_fact"]
    assert serialized["gate_reason"] == "escalation_topic"


def test_prompt_flags_uncertain_fact_only_when_draft_asserts_it():
    """The coach prompt now instructs the model to flag
    uncertain_property_fact only when the draft asserts an unverifiable
    property fact — a deferral that only promises agent follow-up is
    not an escalation."""
    from sms_prompts import build_inbound_reply_analysis_prompt

    prompt = build_inbound_reply_analysis_prompt(
        {"name": "Lead", "phone_number": "+13035550216"},
        [],
        "Is it still available and what's the price?",
    )
    assert "only acknowledges the question" in prompt
    assert "without stating any property fact" in prompt
    assert "not merely what the lead asked about" in prompt


def test_deterministic_decline_leaves_audit_trail(
    staging, two_users, monkeypatch
):
    """Item 8: a non-retryable failure lands in a clearly observable
    blocked state WITH the reason and an audit trail — the
    harness_audit row records action=approval_blocked and the
    gate_reason, so the human can see why it never sent."""
    import db
    from harness.approvals import approve_approval

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550217")
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=424217,
        draft_extra={"escalation_topics": ["fair_housing"]},
    )
    assert approve_approval(u1, approval_id) is not None
    analysis = _analysis_with_topics(["fair_housing"])
    result = _approved_execution(u1, approval_id, analysis, monkeypatch)
    assert result["retryable"] is False
    assert _approval_row(approval_id)["status"] == "blocked"
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT action, gate_decision, gate_reason
            FROM harness_audit
            WHERE user_id = ? AND target_type = 'approval' AND target_id = ?
            ORDER BY id DESC
            """,
            (u1, approval_id),
        ).fetchall()
    blocked = [dict(r) for r in rows if r["action"] == "approval_blocked"]
    assert len(blocked) >= 1, [dict(r) for r in rows]
    assert blocked[0]["gate_reason"] == "escalation_topic"


def test_approved_execution_sends_routine_availability_inquiry(
    staging, two_users, monkeypatch
):
    """Item 10: a routine availability-only question (no price) also
    executes after approval — exactly one mock send."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_phone = "+13035550218"
    lead_id = _staging_lead(u1, lead_phone)
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=424218,
        draft_extra={
            "escalation_topics": ["uncertain_property_fact"],
            "draft_reply": "Hi! Thanks for asking — let me have our agent confirm "
            "whether it's still available and get right back to you.",
        },
    )
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()
    analysis = _analysis_with_topics(
        ["uncertain_property_fact"],
        draft="Hi! Thanks for asking — let me have our agent confirm "
        "whether it's still available and get right back to you.",
    )
    analysis["intent"] = "Property inquiry - availability"
    result = _approved_execution(u1, approval_id, analysis, monkeypatch)
    assert result["sent"] is True, result
    assert _approval_row(approval_id)["status"] == "executed"
    recorded = sent_messages()
    assert len(recorded) == 1
    assert recorded[0]["to"] == lead_phone
