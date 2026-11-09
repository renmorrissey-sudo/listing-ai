"""D5 strict write-tool argument validation + L2 thread-fallback removal.

Covers ``harness.validation`` (missing lead identifiers, invalid status
values, nonexistent leads/resources, cross-tenant leads, malformed
parameters — each rejected at enqueue / SMS-job / approve time and
re-validated at execution time), the SMS-job and approval wiring, and
the L2 proof that an enqueue failure produces a visible failure record
with zero provider calls and zero thread-path execution.

All provider/model calls are mocked; nothing here sends a real SMS.
"""

import json
import sys
import threading
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import db
from harness.validation import (
    ValidationError,
    validate_sms_reply_draft,
    validate_tool_arguments,
)


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id):
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Validation Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    return lead_id, phone


def _nonexistent_lead_id():
    with db.get_db() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1000 AS nid FROM leads").fetchone()
    return int(row["nid"])


def _assert_code(user_id, tool, args, code):
    with pytest.raises(ValidationError) as excinfo:
        validate_tool_arguments(user_id, tool, args)
    assert excinfo.value.code == code


# --- Validator: missing / invalid / nonexistent / cross-tenant ---------------

def test_missing_lead_id_rejected(two_users):
    u1, _ = two_users
    _assert_code(u1, "update_lead_status", {"status": "contacted"}, "missing_lead_id")
    _assert_code(u1, "add_lead_note", {"note": "called"}, "missing_lead_id")
    _assert_code(u1, "send_sms", {"lead_id": None, "body": "Hi"}, "missing_lead_id")


def test_malformed_lead_id_rejected(two_users):
    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    _assert_code(u1, "update_lead_status", {"lead_id": "abc", "status": "contacted"}, "malformed_lead_id")
    _assert_code(u1, "update_lead_status", {"lead_id": 1.5, "status": "contacted"}, "malformed_lead_id")
    _assert_code(u1, "update_lead_status", {"lead_id": True, "status": "contacted"}, "malformed_lead_id")
    _assert_code(u1, "update_lead_status", {"lead_id": "12x", "status": "contacted"}, "malformed_lead_id")
    # A plain digit string is a well-formed id, not a malformed one.
    cleaned = validate_tool_arguments(u1, "update_lead_status", {"lead_id": str(lead_id), "status": "contacted"})
    assert cleaned["lead_id"] == lead_id


def test_nonexistent_lead_rejected(two_users):
    u1, _ = two_users
    missing = _nonexistent_lead_id()
    _assert_code(u1, "update_lead_status", {"lead_id": missing, "status": "contacted"}, "lead_not_found")
    _assert_code(u1, "send_sms", {"lead_id": missing, "body": "Hi"}, "lead_not_found")


def test_cross_tenant_lead_rejected(two_users):
    u1, u2 = two_users
    other_lead, _ = _make_lead(u2)
    _assert_code(u1, "update_lead_status", {"lead_id": other_lead, "status": "contacted"}, "cross_tenant_lead")
    _assert_code(u1, "send_sms", {"lead_id": other_lead, "body": "Hi"}, "cross_tenant_lead")
    # The owning tenant validates fine.
    cleaned = validate_tool_arguments(u2, "update_lead_status", {"lead_id": other_lead, "status": "contacted"})
    assert cleaned["lead_id"] == other_lead


def test_invalid_status_rejected_never_coerced_to_new(two_users):
    """D5 core: an unknown status is rejected, not silently coerced to
    "new" (the old normalize_lead_status behaviour)."""
    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    for bad in ("banana_status", "closed-won!!", 123, ""):
        _assert_code(u1, "update_lead_status", {"lead_id": lead_id, "status": bad},
                     "invalid_status" if bad not in ("", 123) else ("missing_status" if bad == "" else "malformed_parameter"))
    # Valid statuses pass; legacy aliases resolve to canonical slugs.
    assert validate_tool_arguments(
        u1, "update_lead_status", {"lead_id": lead_id, "status": "contacted"})["status"] == "contacted"
    assert validate_tool_arguments(
        u1, "update_lead_status", {"lead_id": lead_id, "status": "hot"})["status"] == "qualified"
    assert validate_tool_arguments(
        u1, "update_lead_status", {"lead_id": lead_id, "status": "closed"})["status"] == "closed_won"
    # A final status is *valid* here — the policy gate (not validation)
    # decides whether it may be auto-applied.
    assert validate_tool_arguments(
        u1, "update_lead_status", {"lead_id": lead_id, "status": "closed_won"})["status"] == "closed_won"


def test_malformed_parameters_rejected(two_users):
    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    # Empty / missing message bodies.
    _assert_code(u1, "send_sms", {"lead_id": lead_id, "body": "   "}, "missing_body")
    _assert_code(u1, "send_sms", {"lead_id": lead_id}, "missing_body")
    _assert_code(u1, "send_email", {"lead_id": lead_id, "body": "Hi"}, "missing_subject")
    # Malformed priority is rejected, not silently replaced with "normal".
    _assert_code(u1, "create_task", {"lead_id": lead_id, "title": "Call", "priority": "banana"}, "invalid_priority")
    # Missing title.
    _assert_code(u1, "create_task", {"lead_id": lead_id, "title": " "}, "missing_title")
    # Malformed due_at.
    _assert_code(u1, "create_follow_up", {"lead_id": lead_id, "due_at": "not-a-date"}, "missing_due_at")
    _assert_code(u1, "create_follow_up", {"lead_id": lead_id}, "missing_due_at")
    # Malformed calendar times; end before start.
    _assert_code(u1, "create_calendar_event", {"lead_id": lead_id, "start_at": "tomorrow-ish"}, "missing_start_at")
    _assert_code(
        u1, "create_calendar_event",
        {"lead_id": lead_id, "start_at": "2026-11-10T15:00:00Z", "end_at": "2026-11-10T14:00:00Z"},
        "malformed_parameter",
    )
    # Nonexistent appointment on reschedule.
    _assert_code(
        u1, "reschedule_calendar_event",
        {"lead_id": lead_id, "appointment_id": 987654321, "start_at": "2026-11-10T15:00:00Z"},
        "appointment_not_found",
    )
    _assert_code(
        u1, "reschedule_calendar_event",
        {"lead_id": lead_id, "start_at": "2026-11-10T15:00:00Z"},
        "missing_appointment_id",
    )
    # create_lead: name/phone required, bad email and bad lead_type rejected.
    _assert_code(u1, "create_lead", {"name": "No Phone"}, "missing_phone")
    _assert_code(u1, "create_lead", {"phone": "+13035551234"}, "missing_name")
    _assert_code(u1, "create_lead", {"name": "X", "phone": "+13035551234", "email": "not-an-email"}, "malformed_email")
    _assert_code(u1, "create_lead", {"name": "X", "phone": "+13035551234", "lead_type": "alien"}, "invalid_lead_type")
    _assert_code(u1, "create_lead", {"name": "X", "phone": "123"}, "malformed_phone")
    # Non-integer numeric fields rejected, not digit-stripped.
    _assert_code(u1, "create_lead", {"name": "X", "phone": "+13035551234", "bedrooms": "three"}, "malformed_parameter")
    # update_lead_contact_info with nothing to update.
    _assert_code(u1, "update_lead_contact_info", {"lead_id": lead_id}, "missing_contact_update")
    # add_lead_note with empty note.
    _assert_code(u1, "add_lead_note", {"lead_id": lead_id, "note": ""}, "missing_note")


def test_restricted_and_unknown_tools_rejected(two_users):
    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    for tool in ("delete_lead", "change_consent", "change_sms_qualification",
                 "sql", "execute_sql", "raw_query", "delete_records",
                 "cancel_subscription", "delete_customer"):
        _assert_code(u1, tool, {"lead_id": lead_id}, "restricted_tool")
    _assert_code(u1, "teleport_lead", {"lead_id": lead_id}, "unknown_tool")
    _assert_code(u1, "", {"lead_id": lead_id}, "unknown_tool")


def test_valid_write_arguments_pass(two_users):
    u1, _ = two_users
    lead_id, phone = _make_lead(u1)
    assert validate_tool_arguments(
        u1, "send_sms", {"lead_id": lead_id, "body": "Hi there"})["body"] == "Hi there"
    assert validate_tool_arguments(
        u1, "create_lead",
        {"name": "New Lead", "phone": "+13035559999", "email": "a@example.com",
         "lead_type": "buyer", "bedrooms": 3})["lead_type"] == "buyer"
    assert validate_tool_arguments(
        u1, "create_task",
        {"lead_id": lead_id, "title": "Call back", "priority": "high"})["priority"] == "high"
    assert validate_tool_arguments(
        u1, "create_follow_up",
        {"lead_id": lead_id, "due_at": "2026-11-10T15:00:00Z", "reason": "Tour"})["due_at"] == "2026-11-10T15:00:00Z"
    assert validate_tool_arguments(
        u1, "create_calendar_event",
        {"lead_id": lead_id, "start_at": "2026-11-10T15:00:00Z",
         "end_at": "2026-11-10T16:00:00Z"})["start_at"] == "2026-11-10T15:00:00Z"
    # Read tools pass through unchanged (no write arguments to validate).
    assert validate_tool_arguments(u1, "find_lead", {"phone": phone}) == {"phone": phone}


# --- SMS job path: validation before drafting --------------------------------

def _run_sms_job(user_id, lead_id, inbound_id, analysis):
    from harness.sms_handler import process_sms_auto_reply_job

    job = {
        "id": 999001,
        "user_id": user_id,
        "payload": {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": "Hi, is the house still available?",
            "receiving_number": "+18880000000",
        },
    }
    with patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": analysis, "insight_id": None, "error": None},
    ):
        return process_sms_auto_reply_job(job)


def _base_analysis(**overrides):
    analysis = {
        "summary": "Lead asked about availability.",
        "intent": "question",
        "draft_reply": "Hi! Yes, it is still available. Want to tour Saturday?",
        "suggested_reply": "Hi! Yes, it is still available. Want to tour Saturday?",
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "suggested_lead_status": "contacted",
    }
    analysis.update(overrides)
    return analysis


def _approvals_for(user_id):
    with db.get_db() as conn:
        return conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ?", (user_id,)
        ).fetchall()


def test_sms_job_rejects_cross_tenant_lead(two_users):
    u1, u2 = two_users
    other_lead, _ = _make_lead(u2)
    result = _run_sms_job(u1, other_lead, 424242, _base_analysis())
    assert result["ok"] is False
    assert result["reason"] == "cross_tenant_lead"
    assert result["sent"] is False
    assert _approvals_for(u1) == []


def test_sms_job_rejects_nonexistent_lead(two_users):
    u1, _ = two_users
    result = _run_sms_job(u1, _nonexistent_lead_id(), 424242, _base_analysis())
    assert result["ok"] is False
    assert result["reason"] == "lead_not_found"
    assert _approvals_for(u1) == []


def test_sms_job_rejects_malformed_lead_id(two_users):
    u1, _ = two_users
    result = _run_sms_job(u1, "abc", 424242, _base_analysis())
    assert result["ok"] is False
    assert result["reason"] == "malformed_lead_id"
    assert _approvals_for(u1) == []


def test_sms_job_invalid_suggested_status_rejected_not_persisted(two_users):
    """An invalid coach-suggested status is stripped from the approval
    draft and recorded — never persisted, never coerced to "new"."""
    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    result = _run_sms_job(u1, lead_id, 424242, _base_analysis(suggested_lead_status="banana_status"))
    assert result["ok"] is True
    assert result["decision"] == "DRAFT"
    assert "invalid suggested_lead_status" in result["reason"]
    assert result["approval_id"] is not None
    row = _approvals_for(u1)[0]
    draft = json.loads(row["draft_json"])
    assert "suggested_lead_status" not in draft or draft["suggested_lead_status"] in (None, "")
    assert draft["draft_reply"].startswith("Hi! Yes")
    # The rejection is audited.
    from harness.audit import list_audit

    reasons = [r["gate_reason"] for r in list_audit(u1)]
    assert any("invalid suggested_lead_status" in (r or "") for r in reasons)


def test_sms_job_valid_suggested_status_persists(two_users):
    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    result = _run_sms_job(u1, lead_id, 424242, _base_analysis(suggested_lead_status="hot"))
    assert result["ok"] is True
    draft = json.loads(_approvals_for(u1)[0]["draft_json"])
    # Legacy alias resolved to its canonical slug by validation.
    assert draft["suggested_lead_status"] == "qualified"


# --- Approvals: validate at approve, revalidate at execute -------------------

def _insert_approval(user_id, lead_id, inbound_id, draft_reply="Hi! Yes, it is still available.",
                     suggested_status=None):
    draft = {"draft_reply": draft_reply, "lead_id": lead_id, "inbound_id": inbound_id}
    if suggested_status is not None:
        draft["suggested_lead_status"] = suggested_status
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_approvals
                (user_id, job_id, kind, draft_json, status, created_at)
            VALUES (?, ?, 'sms_reply', ?, 'pending', ?)
            """,
            (user_id, None, json.dumps(draft), datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def _approval_status(approval_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return row["status"]


def test_approve_rejects_invalid_draft_status(two_users):
    from harness.approvals import approve_approval

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424242, suggested_status="banana_status")
    approval, error = approve_approval(u1, approval_id)
    assert error == "invalid_draft"
    assert _approval_status(approval_id) == "pending"  # not approved, not enqueued


def test_approve_rejects_cross_tenant_draft_lead(two_users):
    from harness.approvals import approve_approval

    u1, u2 = two_users
    other_lead, _ = _make_lead(u2)
    approval_id = _insert_approval(u1, other_lead, 424242)
    _, error = approve_approval(u1, approval_id)
    assert error == "invalid_draft"
    assert _approval_status(approval_id) == "pending"


def test_approve_rejects_missing_draft_reply(two_users):
    from harness.approvals import approve_approval

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424242, draft_reply="   ")
    _, error = approve_approval(u1, approval_id)
    assert error == "invalid_draft"


def test_execute_revalidates_after_lead_deleted(two_users):
    """A job enqueued with valid args that later became invalid (lead
    deleted between approve and execute) fails safely instead of sending."""
    from harness.approvals import approve_approval, execute_approved

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424242)
    _, error = approve_approval(u1, approval_id)
    assert error is None
    # Lead disappears after approval.
    with db.get_db() as conn:
        conn.execute("DELETE FROM leads WHERE id = ? AND user_id = ?", (lead_id, u1))
    assert db.get_lead(lead_id, u1) is None
    with patch("sms_ai_agent.process_inbound_ai") as mock_send:
        result = execute_approved(u1, approval_id)
    assert not mock_send.called
    assert result["sent"] is False
    assert result["decision"] == "BLOCK"
    assert "validation_failed" in result["reason"]
    assert _approval_status(approval_id) == "blocked"


def test_execute_revalidates_invalid_suggested_status(two_users):
    """An invalid suggested status smuggled into a draft (e.g. created
    before D5 validation) blocks execution instead of being coerced."""
    from harness.approvals import execute_approved

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424242, suggested_status="banana_status")
    # Force-approved directly (approve_approval would reject it).
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_approvals SET status = 'approved' WHERE id = ?", (approval_id,)
        )
    with patch("sms_ai_agent.process_inbound_ai") as mock_send:
        result = execute_approved(u1, approval_id)
    assert not mock_send.called
    assert result["sent"] is False
    assert result["decision"] == "BLOCK"
    assert "validation_failed" in result["reason"]
    assert _approval_status(approval_id) == "blocked"


def test_validate_sms_reply_draft_direct(two_users):
    u1, u2 = two_users
    lead_id, _ = _make_lead(u1)
    other_lead, _ = _make_lead(u2)
    cleaned, error = validate_sms_reply_draft(
        u1, {"lead_id": lead_id, "inbound_id": 424242, "draft_reply": "Hi!"})
    assert error is None and cleaned["lead_id"] == lead_id
    _, error = validate_sms_reply_draft(
        u1, {"lead_id": other_lead, "inbound_id": 424242, "draft_reply": "Hi!"})
    assert error == "cross_tenant_lead"
    _, error = validate_sms_reply_draft(
        u1, {"lead_id": lead_id, "inbound_id": 424242, "draft_reply": "Hi!",
             "suggested_lead_status": "nope"})
    assert error == "invalid_status"
    _, error = validate_sms_reply_draft(
        u1, {"inbound_id": 424242, "draft_reply": "Hi!"})
    assert error == "missing_lead_id"


# --- L2: enqueue failure fails closed, visibly, with no thread/send ----------

def test_enqueue_failure_is_visible_and_sends_nothing(two_users):
    """L2: when the durable enqueue raises, schedule_inbound_ai records
    a visible needs-attention failure and does NOT coach or send via
    any ungated path (no thread, no provider call, no job row)."""
    import crm_db
    import sms_ai_agent
    from harness.jobs import list_jobs

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    inbound_id = 424243
    jobs_before = list_jobs(u1)
    with patch("harness.jobs.enqueue_job", side_effect=RuntimeError("queue down")), \
         patch.object(sms_ai_agent, "process_inbound_ai") as mock_process, \
         patch.object(threading, "Thread") as mock_thread, \
         patch("sms_providers.get_sms_provider") as mock_provider:
        sms_ai_agent.schedule_inbound_ai(
            None, u1, lead_id, inbound_id, "Hi, is it available?", "+18880000000"
        )
    assert not mock_process.called
    assert not mock_thread.called
    assert not mock_provider.called
    assert list_jobs(u1) == jobs_before  # no job row was created
    attention = crm_db.list_needs_attention(u1)
    flagged = [a for a in attention if a["lead_id"] == lead_id
               and a["reason_code"] == "ai_reply_enqueue_failed"]
    assert len(flagged) == 1
    assert flagged[0]["status"] == "open"
    assert flagged[0]["priority"] == "high"


def test_enqueue_unavailable_is_visible_and_sends_nothing(two_users):
    """Same fail-closed behaviour when the harness queue module itself
    cannot be imported."""
    import crm_db
    import sms_ai_agent

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    inbound_id = 424244
    with patch.dict(sys.modules, {"harness.jobs": None}), \
         patch.object(sms_ai_agent, "process_inbound_ai") as mock_process, \
         patch.object(threading, "Thread") as mock_thread:
        sms_ai_agent.schedule_inbound_ai(
            None, u1, lead_id, inbound_id, "Hi, is it available?", "+18880000000"
        )
    assert not mock_process.called
    assert not mock_thread.called
    attention = crm_db.list_needs_attention(u1)
    assert any(a["lead_id"] == lead_id and a["reason_code"] == "ai_reply_enqueue_failed"
               for a in attention)


def test_thread_fallback_is_gone_from_source():
    """The fallback function and any threading use are gone from the
    module — no bypass of harness safety controls can remain here."""
    import inspect

    import sms_ai_agent

    source = inspect.getsource(sms_ai_agent)
    assert "_schedule_inbound_ai_thread" not in source
    assert "threading" not in source
    assert not hasattr(sms_ai_agent, "_schedule_inbound_ai_thread")


def test_schedule_refuses_malformed_references(two_users):
    """D5 at enqueue: malformed lead/inbound ids are refused before any
    enqueue attempt (and cannot create a misleading needs-attention row
    without a valid lead)."""
    import sms_ai_agent
    from harness.jobs import list_jobs

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    jobs_before = list_jobs(u1)
    with patch("harness.jobs.enqueue_job") as mock_enqueue:
        sms_ai_agent.schedule_inbound_ai(None, u1, "abc", 424245, "Hi", "+18880000000")
        sms_ai_agent.schedule_inbound_ai(None, u1, lead_id, "xyz", "Hi", "+18880000000")
    assert not mock_enqueue.called
    assert list_jobs(u1) == jobs_before
