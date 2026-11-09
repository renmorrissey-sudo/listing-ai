"""Approved-draft fidelity: the content executed must match the stored
human-approved message.

Ren's additional requirement (2026-11-09): the approved execution
regenerated the AI draft (re-running the coach) rather than sending
the exact text the human approved. The fix: ``execute_approved``
passes the stored draft to ``process_inbound_ai``, which does NOT
re-run the coach for an approved send — it sends the stored text
exactly, subject to the final authorization, consent, opt-out, policy,
and delivery safety checks. If any check requires the text to change,
the send is blocked (never silently modified).

These tests prove:
- The exact stored draft text is the message sent, even when a fresh
  coach run would generate different text.
- No model call happens between human approval and actual execution.
- A long stored draft is sent in full (not truncated to 480 chars).
- The stored escalation topics govern the gate (a hard topic in the
  stored draft blocks; a routine topic is overridden by approval).

Deterministic only: no model, no provider, no network.
"""

from unittest.mock import patch

import pytest

from tests.test_harness_staging_safety import (
    _GateOpen,
    _approval_row,
    _set_settings,
    _staging_approval,
    _staging_lead,
    _staging_mock_config,
    staging,  # noqa: F401  (fixture, re-exported for pytest)
)

_INBOUND_FIDELITY = 424230
_INBOUND_LONG = 424231
_INBOUND_HARD = 424232
_INBOUND_NO_MODEL = 424233


def _execute(u1, approval_id):
    """Run execute_approved with the coach patched to RAISE if called.

    If the coach is re-run during an approved execution, the test
    fails — proving no model-generated change can occur between human
    approval and actual execution.
    """
    from harness.approvals import execute_approved

    def _coach_must_not_run(*args, **kwargs):
        raise AssertionError(
            "coach re-run during approved execution (fidelity violation)"
        )

    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        side_effect=_coach_must_not_run,
    ), patch(
        "sms_coach.analyze_inbound_reply",
        side_effect=_coach_must_not_run,
    ):
        return execute_approved(u1, approval_id)


def test_exact_stored_draft_is_sent_not_a_fresh_coach_draft(
    staging, two_users, monkeypatch
):
    """The message sent is the exact stored approved text. A fresh
    coach run would generate different text (the mock below would
    return it) — but the coach is never called, so the stored text
    is what goes out."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_phone = "+13035550230"
    lead_id = _staging_lead(u1, lead_phone)
    stored_text = (
        "Hi! Yes, the 3-bedroom on Elm Street is still available. "
        "Our agent will call you tomorrow morning to confirm a tour."
    )
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=_INBOUND_FIDELITY,
        draft_extra={
            "draft_reply": stored_text,
            "escalation_topics": ["uncertain_property_fact"],
        },
    )
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()

    result = _execute(u1, approval_id)
    assert result["sent"] is True, result
    assert _approval_row(approval_id)["status"] == "executed"
    recorded = sent_messages()
    assert len(recorded) == 1
    assert recorded[0]["to"] == lead_phone
    # The sent body is EXACTLY the stored approved text — not a
    # regenerated draft.
    assert recorded[0]["body"] == stored_text, recorded[0]


def test_no_model_call_between_approval_and_execution(
    staging, two_users, monkeypatch
):
    """No model call happens between human approval and execution:
    the coach is patched to raise, and the execution still succeeds
    (proving it never calls the model)."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550231")
    approval_id = _staging_approval(u1, lead_id, inbound_id=_INBOUND_NO_MODEL)
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()
    # _execute patches both coach entry points to raise on any call.
    result = _execute(u1, approval_id)
    assert result["sent"] is True, result
    assert len(sent_messages()) == 1


def test_long_stored_draft_is_sent_in_full_not_truncated(
    staging, two_users, monkeypatch
):
    """A stored draft longer than the auto path's 480-char truncation
    is sent in full. The human approved the whole text; truncating it
    would silently modify the approved message."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_phone = "+13035550232"
    lead_id = _staging_lead(u1, lead_phone)
    stored_text = (
        "Hi! Thanks for your interest in the 3-bedroom on Elm Street. "
        "It is still available, and the price is $425,000. The home has "
        "a new roof, an updated kitchen, a large backyard, and a two-car "
        "garage. It is close to schools and parks. Our agent would love "
        "to show it to you this week — would Thursday at 2pm or Friday "
        "at 10am work better for a tour? Please let us know what works "
        "best for your schedule and we will confirm right away. "
        "We look forward to hearing from you soon, and please bring "
        "any questions about the neighborhood, the schools, or the "
        "homeowners association when you visit."
    )
    assert len(stored_text) > 480, len(stored_text)
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=_INBOUND_LONG,
        draft_extra={"draft_reply": stored_text},
    )
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()

    result = _execute(u1, approval_id)
    assert result["sent"] is True, result
    recorded = sent_messages()
    assert len(recorded) == 1
    assert recorded[0]["body"] == stored_text
    assert len(recorded[0]["body"]) == len(stored_text)


def test_stored_hard_escalation_topic_blocks_fidelity_send(
    staging, two_users, monkeypatch
):
    """The stored escalation topics govern the gate: a hard topic
    (legal) in the stored draft blocks the send, even though the
    human approved it — approval must not override hard safety
    restrictions. The approval settles as blocked, never sent."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550233")
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=_INBOUND_HARD,
        draft_extra={"escalation_topics": ["legal"]},
    )
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()

    result = _execute(u1, approval_id)
    assert result["sent"] is False, result
    assert result["reason"] == "escalation_topic"
    assert result["retryable"] is False
    assert _approval_row(approval_id)["status"] == "blocked"
    assert sent_messages() == []
