"""H3 + H4 regression tests.

H3: no verbatim secret may land in harness_jobs.payload_json. Payloads are
redacted at enqueue time, and the SMS handler loads the raw inbound body
from its sms_messages row by inbound_id instead of trusting the payload.

H4: a ``scheduled_follow_up`` job creates a real CRM follow-up (via the
Ask TopAI path) plus an audit row, sends nothing, and succeeds; a missing
lead dead-letters; unknown kinds still dead-letter.

All provider/Anthropic calls are mocked; nothing here sends a real SMS.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import db
from harness.jobs import enqueue_job, redact_payload


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py), so
    earlier tests can leave harness jobs behind. Tests in this file
    drive the worker (run_once), which claims the oldest pending job,
    so clear the queue first: the job the worker picks up is then this
    test's job, not a leftover."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    yield


FAKE_KEY = "sk-test-4f8a2b9c6d1e7f0a3b5c8d9e2f4a6b8c1d3e"
FAKE_PHONE = "303-555-0142"
FAKE_EMAIL = "lead@example.com"


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


def _payload(job_id):
    return json.loads(_row(job_id)["payload_json"])


def _make_lead(user_id, name="Follow Lead"):
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": name, "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    return lead_id, phone


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


def _outbound_count(user_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_messages "
            "WHERE user_id = ? AND direction = 'outbound'",
            (user_id,),
        ).fetchone()
        return int(row["c"])


# --- H3: redaction at enqueue ----------------------------------------------


def test_redact_payload_covers_secret_shapes(two_users):
    assert "[REDACTED]" in redact_payload({"body": f"key is {FAKE_KEY}"})["body"]
    assert FAKE_KEY not in redact_payload({"body": f"key is {FAKE_KEY}"})["body"]
    assert "[REDACTED]" in redact_payload(
        {"body": "sk-ant-api03-abcdefghij1234567890"}
    )["body"]
    assert "[REDACTED]" in redact_payload(
        {"body": "Authorization: Bearer abcdefghij1234567890"}
    )["body"]
    assert "[REDACTED]" in redact_payload(
        {"body": "api_key=abcdefghij1234567890"}
    )["body"]
    assert "[REDACTED]" in redact_payload(
        {"body": "token 0123456789abcdef0123456789abcdef"}
    )["body"]
    # Ordinary text survives untouched.
    assert redact_payload({"body": "Hi, is the house still available?"})[
        "body"
    ] == "Hi, is the house still available?"


def test_enqueue_redacts_api_key_phone_and_email_in_body(two_users):
    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    body = (
        f"Hi, my api key is {FAKE_KEY}, call me at {FAKE_PHONE} "
        f"or email {FAKE_EMAIL}. Is the house still available?"
    )
    job_id = enqueue_job(
        u1,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": 424242,
            "body": body,
            "receiving_number": "+18880000000",
        },
    )
    stored = _row(job_id)["payload_json"]
    assert FAKE_KEY not in stored
    assert "sk-test-" not in stored
    assert FAKE_PHONE not in stored
    assert FAKE_EMAIL not in stored
    assert "[REDACTED]" in stored
    # Structured fields the send path needs are preserved, not mangled.
    assert _payload(job_id)["lead_id"] == lead_id
    assert _payload(job_id)["receiving_number"] == "+18880000000"
    assert "still available" in _payload(job_id)["body"]
    from harness.jobs import complete_job

    complete_job(job_id)


def test_handler_loads_body_from_inbound_row_not_payload(two_users):
    """The job carries NO body at all; the handler must still coach on the
    raw inbound body loaded from sms_messages, and payload_json must hold
    no verbatim secret."""
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id, phone = _make_lead(u1)
    secret_body = f"Hi, my key is {FAKE_KEY}, call {FAKE_PHONE} please."
    inbound_id = db.create_sms_message(
        u1, None, "telnyx",
        {"phone_number": phone, "message_body": secret_body},
        status="received", lead_id=lead_id, direction="inbound",
    )
    job_id = enqueue_job(
        u1,
        "sms_auto_reply",
        {"lead_id": lead_id, "inbound_id": inbound_id,
         "receiving_number": "+18880000000"},
    )
    assert FAKE_KEY not in _row(job_id)["payload_json"]
    assert "body" not in _payload(job_id)

    with patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": dict(_ANALYSIS), "insight_id": None, "error": None},
    ) as mock_coach, patch(
        "sms_ai_agent.process_inbound_ai"
    ) as mock_send, patch(
        "sms_quiet_hours.in_quiet_hours", return_value=False
    ):
        assert run_once() is True
        assert not mock_send.called  # drafts_only: no send
        # The coach saw the RAW body from the sms_messages row.
        assert mock_coach.call_count == 1
        assert mock_coach.call_args[0][3] == secret_body
    assert _row(job_id)["status"] == "succeeded"
    with db.get_db() as conn:
        approvals = conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ? AND status = 'pending'",
            (u1,),
        ).fetchall()
    assert len(approvals) == 1


# --- H4: scheduled_follow_up -------------------------------------------------


def test_scheduled_follow_up_creates_follow_up_and_audit(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    due_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    reason = "Call back about the Main St listing"
    job_id = enqueue_job(
        u1,
        "scheduled_follow_up",
        {"lead_id": lead_id, "due_at": due_at, "reason": reason,
         "priority": "normal"},
    )
    assert run_once() is True
    assert _row(job_id)["status"] == "succeeded"
    assert _row(job_id)["result_json"]
    assert json.loads(_row(job_id)["result_json"]).get("follow_up_id")

    import crm_db

    follow_ups = crm_db.list_lead_follow_ups(u1, lead_id)
    assert len(follow_ups) == 1
    assert follow_ups[0]["reason"] == reason
    assert follow_ups[0]["status"] == "pending"

    from harness.audit import list_audit

    created = [
        r for r in list_audit(u1)
        if r["action"] == "scheduled_follow_up_created"
        and r["tool"] == "scheduled_follow_up"
    ]
    assert len(created) == 1
    assert created[0]["target_id"] == lead_id
    # A follow-up is a CRM record, not a message: nothing was sent.
    assert _outbound_count(u1) == 0


def test_scheduled_follow_up_defaults_due_at_when_missing(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id, _phone = _make_lead(u1)
    job_id = enqueue_job(
        u1, "scheduled_follow_up",
        {"lead_id": lead_id, "reason": "Check in tomorrow"},
    )
    assert run_once() is True
    assert _row(job_id)["status"] == "succeeded"
    import crm_db

    follow_ups = crm_db.list_lead_follow_ups(u1, lead_id)
    assert len(follow_ups) == 1
    assert follow_ups[0]["due_at"]


def test_scheduled_follow_up_missing_lead_dead_letters(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(
        u1, "scheduled_follow_up",
        {"lead_id": 999999999, "due_at": "2026-11-10T10:00:00+00:00",
         "reason": "No such lead"},
    )
    assert run_once() is True
    row = _row(job_id)
    assert row["status"] == "dead_letter"
    assert "lead_missing" in (row["result_json"] or "")


def test_scheduled_follow_up_missing_lead_id_dead_letters(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "scheduled_follow_up", {"reason": "No lead given"})
    assert run_once() is True
    assert _row(job_id)["status"] == "dead_letter"


def test_unknown_kind_still_dead_letters(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "definitely_not_a_kind", {"x": 1})
    assert run_once() is True
    row = _row(job_id)
    assert row["status"] == "dead_letter"
    assert "unknown_kind" in (row["result_json"] or "")
