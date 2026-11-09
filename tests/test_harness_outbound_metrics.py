"""Outbound metrics and reporting (defect 4 remediation).

Root cause: a mock approved execution succeeded, but the dashboard
outbound counters stayed zero. The mock provider records the message
with status ``mock_sent`` (not ``sent``), and the approved path audits
``approval_executed`` (not ``sms_auto_reply_sent``) — and neither
metric counted those. So a successful execution was invisible.

The fix tracks mock execution in SEPARATE test metrics
(``sms_mock_sent_60d``, ``approvals_executed_60d``) while the real
send metrics (``sms_sent_60d``, ``sms_messages_sent_60d``) count only
real provider sends. ``approvals_blocked_60d`` makes deterministic
declines observable (defect 1 item 8), and ``sms_delivered_60d``
counts confirmed deliveries. These tests drive a real mock approved
execution and assert the metrics split correctly.
"""

from unittest.mock import patch

import db
from harness.routes import get_harness_status
from tests.test_harness_staging_safety import (
    _GateOpen,
    _set_settings,
    _staging_analysis,
    _staging_approval,
    _staging_lead,
    _staging_mock_config,
    staging,  # noqa: F401  (fixture, re-exported for pytest)
)


def _execute_mock_approval(u1, approval_id, analysis, monkeypatch):
    from harness.approvals import execute_approved

    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": analysis, "insight_id": None, "error": None},
    ):
        return execute_approved(u1, approval_id)


def _routine_analysis():
    analysis = _staging_analysis()
    analysis["escalation_topics"] = ["uncertain_property_fact"]
    analysis["sensitive_topic"] = True
    analysis["requires_manual_review"] = True
    return analysis


def test_mock_execution_appears_in_mock_metrics_not_real(
    staging, two_users, monkeypatch
):
    """Defect 4 acceptance: after exactly one mock approved execution,
    the mock metrics show 1 and the real outbound customer
    communications stay zero."""
    from harness.approvals import approve_approval
    from sms_providers.mock import clear_sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550220")
    approval_id = _staging_approval(u1, lead_id, inbound_id=424220)
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()

    result = _execute_mock_approval(u1, approval_id, _routine_analysis(), monkeypatch)
    assert result["sent"] is True, result

    activity = get_harness_status(u1)["outbound_activity"]
    # Mock execution is visible in the mock/test metrics.
    assert activity["sms_mock_sent_60d"] == 1, activity
    assert activity["approvals_executed_60d"] == 1, activity
    # Real customer communications stay zero.
    assert activity["sms_sent_60d"] == 0, activity
    assert activity["sms_messages_sent_60d"] == 0, activity
    assert activity["sms_delivered_60d"] == 0, activity
    # No failures or blocks from this execution.
    assert activity["approvals_blocked_60d"] == 0, activity
    assert activity["sms_send_failed_60d"] == 0, activity


def test_blocked_approval_appears_in_blocked_metric(
    staging, two_users, monkeypatch
):
    """A deterministic decline (hard escalation topic) settles as
    blocked and shows in approvals_blocked_60d — observable, with no
    mock or real send recorded."""
    from harness.approvals import approve_approval

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550221")
    approval_id = _staging_approval(
        u1, lead_id, inbound_id=424221,
        draft_extra={"escalation_topics": ["legal"]},
    )
    assert approve_approval(u1, approval_id) is not None
    analysis = _routine_analysis()
    analysis["escalation_topics"] = ["legal"]
    result = _execute_mock_approval(u1, approval_id, analysis, monkeypatch)
    assert result["sent"] is False
    assert result["retryable"] is False

    activity = get_harness_status(u1)["outbound_activity"]
    assert activity["approvals_blocked_60d"] == 1, activity
    assert activity["sms_mock_sent_60d"] == 0, activity
    assert activity["approvals_executed_60d"] == 0, activity
    assert activity["sms_sent_60d"] == 0, activity
    assert activity["sms_messages_sent_60d"] == 0, activity


def test_retry_does_not_double_count_mock_metric(
    staging, two_users, monkeypatch
):
    """A repeated execution attempt for the same approval (already
    executed) sends nothing more and does not double-count the mock
    metric — exactly-once holds across retries."""
    from harness.approvals import approve_approval, execute_approved
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550222")
    approval_id = _staging_approval(u1, lead_id, inbound_id=424222)
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()

    first = _execute_mock_approval(u1, approval_id, _routine_analysis(), monkeypatch)
    assert first["sent"] is True
    # A second execution attempt for the same approval.
    second = execute_approved(u1, approval_id)
    assert second["sent"] is False
    assert second["decision"] == "ALREADY_EXECUTED"

    assert len(sent_messages()) == 1
    activity = get_harness_status(u1)["outbound_activity"]
    assert activity["sms_mock_sent_60d"] == 1, activity
    assert activity["approvals_executed_60d"] == 1, activity
    assert activity["sms_sent_60d"] == 0, activity
    assert activity["sms_messages_sent_60d"] == 0, activity


def test_delivered_status_counts_as_confirmed_delivery(two_users):
    """Confirmed deliveries (message status 'delivered', set by the
    provider delivery callback) count in sms_delivered_60d, not in
    the mock metric."""
    from datetime import datetime, timezone

    u1, _ = two_users
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO sms_messages
                (user_id, phone_number, message_body, direction, status, created_at)
            VALUES (?, '+13035550223', 'delivered message', 'outbound', 'delivered', ?)
            """,
            (u1, now),
        )
    activity = get_harness_status(u1)["outbound_activity"]
    assert activity["sms_delivered_60d"] == 1, activity
    assert activity["sms_mock_sent_60d"] == 0, activity
