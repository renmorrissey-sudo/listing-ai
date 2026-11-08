"""Phase 1 harness audit log tests (harness/audit.py).

The audit log is append-only by design: this module exposes record/list
and deliberately no update/delete API, and the tests assert that.
"""

import pytest

from harness import audit
from harness.audit import list_audit, record_audit


def test_record_list_round_trip(two_users):
    u1, _ = two_users
    row_id = record_audit(u1, "ai", "orchestrator.run", tool="send_sms")
    assert isinstance(row_id, int)
    rows = list_audit(u1)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == row_id
    assert row["user_id"] == u1
    assert row["actor"] == "ai"
    assert row["action"] == "orchestrator.run"
    assert row["tool"] == "send_sms"


def test_list_is_newest_first(two_users):
    u1, _ = two_users
    first = record_audit(u1, "user", "action.one")
    second = record_audit(u1, "user", "action.two")
    rows = list_audit(u1)
    assert [r["id"] for r in rows] == [second, first]


def test_list_is_user_scoped(two_users):
    u1, u2 = two_users
    record_audit(u1, "ai", "only-for-u1")
    assert list_audit(u2) == []
    assert len(list_audit(u1)) == 1


def test_gate_decision_and_reason_recorded(two_users):
    u1, _ = two_users
    record_audit(
        u1,
        "ai",
        "sms_auto_reply_drafted",
        tool="sms_auto_reply",
        target_type="lead",
        target_id=42,
        mode="drafts_only",
        gate_decision="DRAFT",
        gate_reason="drafts_only mode: sends are drafts, never auto-sent",
        cost_cents=3,
        request_id="req-1",
        correlation_id="corr-1",
    )
    row = list_audit(u1)[0]
    assert row["gate_decision"] == "DRAFT"
    assert row["gate_reason"] == "drafts_only mode: sends are drafts, never auto-sent"
    assert row["mode"] == "drafts_only"
    assert row["target_type"] == "lead"
    assert row["target_id"] == 42
    assert row["cost_cents"] == 3
    assert row["request_id"] == "req-1"
    assert row["correlation_id"] == "corr-1"


def test_audit_module_exposes_no_update_or_delete_api():
    """Append-only: there must be no update/delete/remove function to call."""
    for name in (
        "update_audit",
        "update",
        "delete_audit",
        "delete",
        "remove_audit",
        "remove",
        "edit_audit",
    ):
        assert not hasattr(audit, name), f"audit module must not expose {name}"


def test_audit_rows_are_immutable_through_the_module(two_users):
    # The only write path is record_audit (insert); there is no API that
    # takes an existing row id and changes it, so a recorded decision
    # cannot be rewritten after the fact.
    u1, _ = two_users
    row_id = record_audit(u1, "ai", "gate.check", gate_decision="BLOCK")
    assert list_audit(u1)[0]["id"] == row_id
    assert list_audit(u1)[0]["gate_decision"] == "BLOCK"
    assert not hasattr(audit, "update_audit")
