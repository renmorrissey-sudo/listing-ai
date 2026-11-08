"""C2 regression tests: tenant rate limits must gate the harness SMS path.

An authorized-mode ``sms_auto_reply`` job with the tenant 30/min limit
tripped must DEFER and be re-queued retryably (zero provider calls, job
still pending) -- never send, never silently complete, never dead-letter.
Once the limit clears, the same job must send exactly once.

Quiet-hours DEFERs keep the old behaviour (completed, no send, no retry)
and Drafts Only tenants are unaffected by a tripped limit.

All provider/Anthropic calls are mocked; nothing here sends a real SMS.
"""

from contextlib import contextmanager
from unittest.mock import patch

import db
from harness.jobs import enqueue_job
from harness.sms_handler import _decide_policy


def _set_settings(user_id, mode="drafts_only", paused=False, ai_enabled=True):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO harness_settings
                (user_id, mode, paused, ai_enabled)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, mode, 1 if paused else 0, 1 if ai_enabled else 0),
        )


def _unique_phone(prefix="303"):
    import uuid

    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _make_due(job_id):
    """Force a re-queued job's next_attempt_at into the past (time passing)."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET next_attempt_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job_id,),
        )


_ANALYSIS = {
    "summary": "Lead asked about availability.",
    "intent": "question",
    "draft_reply": "Hi! Yes, it is still available. Want to tour Saturday?",
    "suggested_reply": "Hi! Yes, it is still available. Want to tour Saturday?",
    "sensitive_topic": False,
    "requires_manual_review": False,
    "escalation_topics": [],
    "suggested_lead_status": "contacted",
}


def _make_lead_with_inbound(user_id, body="Hi, is the house still available?"):
    """Create a lead plus a real inbound sms_messages row (the source of
    truth for the body). Returns (lead_id, phone, inbound_id)."""
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Rate Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    inbound_id = db.create_sms_message(
        user_id, None, "telnyx",
        {"phone_number": phone, "message_body": body},
        status="received", lead_id=lead_id, direction="inbound",
    )
    return lead_id, phone, inbound_id


def _insert_outbound(user_id, lead_id, phone, count):
    """Insert ``count`` outbound sms_messages rows in the last minute, as
    the acceptance test did -- these are what count_sends_since counts."""
    for _ in range(count):
        db.create_sms_message(
            user_id, None, "telnyx",
            {"phone_number": phone, "message_body": "outbound"},
            status="sent", lead_id=lead_id, direction="outbound",
        )


def _delete_outbound(user_id):
    with db.get_db() as conn:
        conn.execute(
            "DELETE FROM sms_messages WHERE user_id = ? AND direction = 'outbound'",
            (user_id,),
        )


def _enqueue_sms_job(user_id, lead_id, inbound_id):
    return enqueue_job(
        user_id,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": "Hi, is the house still available?",
            "receiving_number": "+18880000000",
        },
    )


@contextmanager
def _mocked_coach_and_send(*, quiet_hours=False, replied=True):
    """Mock the coach analysis, the provider send path, and quiet hours
    (False by default so the test is time-of-day independent)."""
    with patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": dict(_ANALYSIS), "insight_id": None, "error": None},
    ), patch(
        "sms_ai_agent.process_inbound_ai",
        return_value={"replied": replied, "message_id": 1},
    ) as mock_send, patch(
        "sms_quiet_hours.in_quiet_hours", return_value=quiet_hours
    ):
        yield mock_send


# --- C2: tripped limit defers retryably, then sends exactly once ----------


def test_authorized_rate_limited_job_requeues_then_sends_once(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    _set_settings(u1, mode="authorized")
    lead_id, phone, inbound_id = _make_lead_with_inbound(u1)
    _insert_outbound(u1, lead_id, phone, 30)  # trips the 30/min tenant limit
    job_id = _enqueue_sms_job(u1, lead_id, inbound_id)

    # Attempt 1: rate limit tripped -> DEFER, retryable, zero provider calls.
    with _mocked_coach_and_send() as mock_send:
        assert run_once() is True
        assert not mock_send.called
    row = _row(job_id)
    assert row["status"] == "pending"  # re-queued, not completed/dead
    assert row["next_attempt_at"]  # backoff window set
    assert row["attempts"] == 1
    assert "rate limit" in (row["result_json"] or "").lower()
    from harness.audit import list_audit

    deferred = [
        r for r in list_audit(u1)
        if r["action"] == "sms_auto_reply_deferred" and r["gate_decision"] == "DEFER"
    ]
    assert len(deferred) == 1
    assert "rate limit" in (deferred[0]["gate_reason"] or "").lower()
    # Not claimable again until its backoff window passes.
    from harness.jobs import claim_jobs

    assert claim_jobs(limit=10) == []

    # Limit clears; time passes; the same job now sends exactly once.
    _delete_outbound(u1)
    _make_due(job_id)
    with _mocked_coach_and_send() as mock_send:
        assert run_once() is True
        assert mock_send.call_count == 1
    assert _row(job_id)["status"] == "succeeded"
    sent = [
        r for r in list_audit(u1)
        if r["action"] == "sms_auto_reply_sent" and r["gate_decision"] == "ALLOW"
    ]
    assert len(sent) == 1


# --- Other DEFER causes keep the old behaviour ------------------------------


def test_quiet_hours_defer_still_completes_without_retry(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    _set_settings(u1, mode="authorized")
    lead_id, phone, inbound_id = _make_lead_with_inbound(u1)
    job_id = _enqueue_sms_job(u1, lead_id, inbound_id)

    with _mocked_coach_and_send(quiet_hours=True) as mock_send:
        assert run_once() is True
        assert not mock_send.called
    row = _row(job_id)
    assert row["status"] == "succeeded"  # completed, NOT re-queued
    assert row["attempts"] == 1
    from harness.audit import list_audit

    deferred = [
        r for r in list_audit(u1)
        if r["action"] == "sms_auto_reply_deferred" and r["gate_decision"] == "DEFER"
    ]
    assert len(deferred) == 1
    assert "quiet" in (deferred[0]["gate_reason"] or "").lower()


def test_drafts_only_ignores_tripped_rate_limit(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    # Default mode is drafts_only; no _set_settings call needed.
    lead_id, phone, inbound_id = _make_lead_with_inbound(u1)
    _insert_outbound(u1, lead_id, phone, 30)  # limit tripped, must not matter
    job_id = _enqueue_sms_job(u1, lead_id, inbound_id)

    with _mocked_coach_and_send() as mock_send:
        assert run_once() is True
        assert not mock_send.called
    assert _row(job_id)["status"] == "succeeded"
    with db.get_db() as conn:
        approvals = conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ? AND status = 'pending'",
            (u1,),
        ).fetchall()
    assert len(approvals) == 1
    _delete_outbound(u1)


# --- The rate check itself fails closed --------------------------------------


def test_rate_check_error_fails_closed_to_defer(two_users):
    u1, _ = two_users
    lead_id, phone, _inbound_id = _make_lead_with_inbound(u1)
    lead = db.get_lead(lead_id, u1)
    with patch(
        "sms_authorization._rate_limited", side_effect=RuntimeError("db down")
    ), patch("sms_quiet_hours.in_quiet_hours", return_value=False):
        decision, reason, rate_limited = _decide_policy(
            u1, lead, dict(_ANALYSIS), "draft", "authorized", False
        )
    assert decision == "DEFER"
    assert "rate limit" in reason.lower()
    assert rate_limited is True
