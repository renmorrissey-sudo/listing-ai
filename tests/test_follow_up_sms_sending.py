"""Phase III-1 gated follow-up sending + scheduling tests.

Every test enqueues via ``schedule_follow_up_sms`` (not raw
``enqueue_job``) wherever the scheduled flow is under test, runs the
job through the real harness worker (``run_once``), and asserts on
durable ROWS (sms_messages, harness_audit, harness_costs,
harness_follow_up_sends, harness_approvals) — never on dashboard
counters.

Setup follows the manual-send precedent
(tests/test_sms_test_gated.py): mock provider, simpletexting, a
verified tenant sender + accepted SMS terms, quiet hours disabled
unless the test is about quiet hours, and the gate inputs pinned open
unless the test is about that input (both ``in_quiet_hours`` lookup
sites patched where quiet hours is under test).
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import config
import db
import tenant_sms_db as tdb
from harness import costs
from harness.followup_sends import (
    FollowUpScheduleError,
    schedule_follow_up_sms,
)
from sms_providers.mock import clear_sent_messages, sent_messages
from tests.test_harness_staging_safety import _set_settings
from workers.harness_worker import run_once

BODY = "Hi Sarah, just checking in on the Maple St showing — still interested in a second tour?"
REASON = "3-day check-in after showing"
REASON_OTHER = "1-week price-drop alert"


@pytest.fixture(autouse=True)
def _isolate_job_queue_and_mock():
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_follow_up_sends")
    clear_sent_messages()
    yield
    clear_sent_messages()


def _now():
    return datetime.now(timezone.utc)


def _iso_in(minutes=0, seconds=0):
    return (_now() + timedelta(minutes=minutes, seconds=seconds)).isoformat()


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _mock_env(monkeypatch):
    """Mock provider for every send; simpletexting so the Telnyx
    toll-free and trial gates pass without Telnyx credentials."""
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)


def _enable_sending(user_id):
    """A verified tenant sender + accepted SMS terms, the two things
    ``send_authorized_sms`` requires before the provider call."""
    tdb.upsert_tenant_sender(
        user_id,
        sender_number=_unique_phone("888"),
        sms_provider="simpletexting",
        sms_enabled=True,
        registration_status="verified",
    )
    tdb.accept_sms_terms(user_id, user_id)


def _make_lead(user_id, consent_status="verified", opt_out_status="active",
               lead_status="new"):
    """A lead with explicit consent state (set directly — the upsert
    helper does not carry sms_consent_status through)."""
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Follow-up Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = ?, opt_out_status = ?, status = ? "
            "WHERE id = ? AND user_id = ?",
            (consent_status, opt_out_status, lead_status, lead_id, user_id),
        )
    return lead_id, phone


def _make_follow_up(user_id, lead_id, due_at=None, reason=REASON):
    import crm_db

    result, error = crm_db.set_lead_follow_up(
        user_id, lead_id, due_at or _iso_in(minutes=60), reason,
        priority="normal", created_by=user_id,
    )
    assert error is None, error
    return result["follow_up_id"]


@contextmanager
def _gate_open():
    """Force the gate inputs open (no quiet hours, no rate limit, not
    suppressed) so results do not depend on the time of day. Quiet
    hours needs TWO patch targets (see tests/test_sms_test_gated.py)."""
    with patch("sms_quiet_hours.in_quiet_hours", return_value=False), patch(
        "sms_authorization.in_quiet_hours", return_value=False
    ), patch("sms_authorization._rate_limited", return_value=False), patch(
        "tenant_sms_db.is_suppressed", return_value=False
    ):
        yield


def _schedule(user_id, lead_id, follow_up_id=None, body=BODY,
              scheduled_at=None, reason=REASON):
    return schedule_follow_up_sms(
        user_id, lead_id, follow_up_id, body,
        scheduled_at or _now().isoformat(), reason,
    )


def _job_row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _job_result(job_id):
    raw = _job_row(job_id).get("result_json")
    return json.loads(raw) if raw else {}


def _send_row(job_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_follow_up_sends WHERE job_id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def _send_rows_for_lead(user_id, lead_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_follow_up_sends WHERE user_id = ? AND lead_id = ?",
                (user_id, lead_id),
            ).fetchall()
        ]


def _sms_row(message_id, user_id):
    return db.get_sms_message(message_id, user_id)


def _audit_rows(user_id, action=None):
    with db.get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_audit WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        ]
    if action:
        rows = [r for r in rows if r["action"] == action]
    return rows


def _approvals(user_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_approvals WHERE user_id = ?", (user_id,)
            ).fetchall()
        ]


def _approval_row(approval_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return dict(row) if row else None


def _needs_attention_rows(user_id, lead_id, reason_code=None):
    with db.get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM needs_attention WHERE user_id = ? AND lead_id = ? "
                "AND status = 'open'",
                (user_id, lead_id),
            ).fetchall()
        ]
    if reason_code:
        rows = [r for r in rows if r["reason_code"] == reason_code]
    return rows


def _cost_rows(user_id, tool=None):
    rows = [dict(r) for r in costs.list_costs(user_id)]
    if tool:
        rows = [r for r in rows if r["tool"] == tool]
    return rows


def _follow_up_row(follow_up_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_follow_ups WHERE id = ?", (follow_up_id,)
        ).fetchone()
    return dict(row) if row else None


def _set_job_due(job_id, when_iso):
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET next_attempt_at = ? WHERE id = ?",
            (when_iso, job_id),
        )


def _run_due(user_id, lead_id, follow_up_id=None, **schedule_kwargs):
    """Schedule (due now) and run the worker once under an open gate."""
    scheduled = _schedule(user_id, lead_id, follow_up_id, **schedule_kwargs)
    with _gate_open():
        assert run_once() is True, "worker ran nothing for the enqueued job"
    return scheduled


# --- Migration / jobs primitives ----------------------------------------------


def test_migration_045_creates_follow_up_sends_table(two_users):
    """Migration 045: the ledger table, its columns, and its indexes."""
    with db.get_db() as conn:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(harness_follow_up_sends)").fetchall()}
        indexes = {r["name"] for r in conn.execute(
            "PRAGMA index_list(harness_follow_up_sends)").fetchall()}
        stamped = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            ("045_follow_up_sms_sends",),
        ).fetchone()
    assert {
        "id", "user_id", "lead_id", "follow_up_id", "job_id", "reason",
        "reason_norm", "sms_message_id", "status", "scheduled_at",
        "sent_at", "provider_message_id", "approval_id", "deferred_count",
        "last_gate_reason", "created_at", "updated_at",
    } <= cols
    assert {
        "idx_follow_up_sends_user_job", "idx_follow_up_sends_due",
        "idx_follow_up_sends_lead_reason", "idx_follow_up_sends_lead",
    } <= indexes
    assert stamped is not None


def test_enqueue_job_next_attempt_at_parks_future_job(two_users):
    """enqueue_job(next_attempt_at=future) parks the job; the worker
    does not claim it until its time arrives."""
    from harness.jobs import claim_jobs, enqueue_job

    u1, _ = two_users
    future_id = enqueue_job(
        u1, "scheduled_follow_up", {"lead_id": 1},
        next_attempt_at=_iso_in(minutes=60),
    )
    assert claim_jobs(limit=5, claimed_by="w1") == []
    assert _job_row(future_id)["status"] == "pending"
    _set_job_due(future_id, _iso_in(minutes=-1))
    assert [j["id"] for j in claim_jobs(limit=5, claimed_by="w1")] == [future_id]


def test_reschedule_job_resets_attempts_and_requires_claimed(two_users):
    """reschedule_job moves a claimed job back to pending at the new
    fire time and resets attempts to 0 (a deferral is not a failure);
    it refuses a job that is not claimed."""
    from harness.jobs import claim_jobs, enqueue_job, reschedule_job

    u1, _ = two_users
    job_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 1})
    with pytest.raises(RuntimeError):
        reschedule_job(job_id, _iso_in(minutes=30), user_id=u1)
    claimed = claim_jobs(limit=5, claimed_by="w1")
    assert [j["id"] for j in claimed] == [job_id]
    assert _job_row(job_id)["attempts"] == 1
    fire_at = reschedule_job(job_id, _iso_in(minutes=30), user_id=u1)
    row = _job_row(job_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == fire_at
    assert row["claimed_at"] is None and row["claimed_by"] is None


# --- Criterion 1: fires at/after scheduled time; Authorized sends ----------


def test_follow_up_fires_only_at_or_after_scheduled_at(two_users, monkeypatch):
    """A job enqueued for the future sits pending until its time, then
    fires exactly once — the next_attempt_at gate, not 'eventually'."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, phone = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(
        u1, lead_id, follow_up_id, scheduled_at=_iso_in(minutes=60)
    )
    with _gate_open():
        assert run_once() is False  # nothing due
    assert _job_row(scheduled["job_id"])["status"] == "pending"
    assert sent_messages() == []
    _set_job_due(scheduled["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert _job_row(scheduled["job_id"])["status"] == "succeeded"
    assert _job_result(scheduled["job_id"])["sent"] is True
    assert len(sent_messages()) == 1
    assert sent_messages()[0]["to"] == phone
    assert sent_messages()[0]["body"] == BODY


def test_authorized_follow_up_sends_via_mock_provider(two_users, monkeypatch):
    """Authorized tenant + consenting lead + open gate: exactly one
    mock send of the exact stored body; job, sms row, audit, cost,
    CRM follow-up and lead stamp all show the send; no approval."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, phone = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)

    job = _job_row(scheduled["job_id"])
    assert job["status"] == "succeeded"
    result = _job_result(scheduled["job_id"])
    assert result["sent"] is True
    assert result["decision"] == "ALLOW"
    assert result["message_id"] == scheduled["message_id"]
    assert result["provider_message_id"].startswith("mock-")

    sends = sent_messages()
    assert len(sends) == 1
    assert sends[0]["to"] == phone
    assert sends[0]["body"] == BODY

    sms = _sms_row(scheduled["message_id"], u1)
    assert sms["status"] in ("sent", "queued", "mock_sent")
    assert sms["provider_message_id"] == result["provider_message_id"]
    assert sms["message_body"] == BODY
    assert sms["direction"] == "outbound"
    assert sms["lead_id"] == lead_id

    send_row = _send_row(scheduled["job_id"])
    assert send_row["status"] == "sent"
    assert send_row["provider_message_id"] == result["provider_message_id"]
    assert send_row["sent_at"] is not None

    sent_audit = _audit_rows(u1, action="follow_up_sms_sent")
    assert len(sent_audit) == 1
    assert sent_audit[0]["gate_decision"] == "ALLOW"
    assert sent_audit[0]["target_type"] == "lead"
    assert sent_audit[0]["target_id"] == lead_id

    follow_up_costs = _cost_rows(u1, tool="follow_up_sms_send")
    assert len(follow_up_costs) == 1
    assert follow_up_costs[0]["cost_cents"] == 2  # 1 segment x simpletexting 2c

    assert _follow_up_row(follow_up_id)["status"] == "done"
    assert _follow_up_row(follow_up_id)["completed_at"] is not None
    assert db.get_lead(lead_id, u1)["last_outbound_at"] is not None
    assert _approvals(u1) == []


def test_drafts_only_follow_up_creates_draft_and_approval(two_users, monkeypatch):
    """Stored mode drafts_only: zero sends, the sms row stays a draft,
    the job settles DRAFT, one pending follow_up_sms approval holds
    the exact stored text, and the audit row records the draft."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="drafts_only", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)

    assert sent_messages() == []
    sms = _sms_row(scheduled["message_id"], u1)
    assert sms["status"] == "draft"
    assert sms["message_body"] == BODY
    job = _job_row(scheduled["job_id"])
    assert job["status"] == "succeeded"
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "DRAFT"
    assert result["sent"] is False
    assert result["approval_id"] is not None

    approvals = _approvals(u1)
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["kind"] == "follow_up_sms"
    assert approval["status"] == "pending"
    draft = json.loads(approval["draft_json"])
    assert draft["draft_reply"] == BODY
    assert draft["lead_id"] == lead_id
    assert draft["follow_up_id"] == follow_up_id
    assert draft["message_id"] == scheduled["message_id"]

    assert _send_row(scheduled["job_id"])["status"] == "drafted"
    drafted = _audit_rows(u1, action="follow_up_sms_drafted")
    assert len(drafted) == 1
    assert drafted[0]["gate_decision"] == "DRAFT"
    assert _follow_up_row(follow_up_id)["status"] == "pending"


def test_drafts_only_approval_executes_exactly_one_mock_send(two_users, monkeypatch):
    """Approving the drafts_only follow-up approval executes exactly
    one mock send of the exact approved text; approval executed."""
    from harness.approvals import approve_approval, enqueue_execute_approval_job

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="drafts_only", paused=False)
    lead_id, phone = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)
    approval_id = _job_result(scheduled["job_id"])["approval_id"]

    approval, error = approve_approval(u1, approval_id)
    assert error is None, error
    assert approval["status"] == "approved"
    exec_job_id = enqueue_execute_approval_job(u1, approval_id)
    assert exec_job_id is not None
    clear_sent_messages()
    with _gate_open():
        assert run_once() is True
    sends = sent_messages()
    assert len(sends) == 1
    assert sends[0]["to"] == phone
    assert sends[0]["body"] == BODY
    assert _approval_row(approval_id)["status"] == "executed"
    executed = _audit_rows(u1, action="approval_executed")
    assert len(executed) == 1
    assert _job_row(exec_job_id)["status"] == "succeeded"
    assert _job_result(exec_job_id)["sent"] is True


def test_paused_tenant_drafts_not_sends(two_users, monkeypatch):
    """Authorized + paused: DRAFT + approval, zero sends."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=True)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)
    assert sent_messages() == []
    assert _job_result(scheduled["job_id"])["decision"] == "DRAFT"
    assert len(_approvals(u1)) == 1
    assert _approvals(u1)[0]["kind"] == "follow_up_sms"


def test_supervised_without_template_drafts(two_users, monkeypatch):
    """Supervised mode with template_approved=False (always, for
    follow-ups): DRAFT + approval, zero sends."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="supervised", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)
    assert sent_messages() == []
    assert _job_result(scheduled["job_id"])["decision"] == "DRAFT"
    assert len(_approvals(u1)) == 1


# --- Criterion 2: gate enforced -----------------------------------------------


def test_opted_out_lead_blocks_no_send_no_draft(two_users, monkeypatch):
    """Lead opted out after scheduling: BLOCK, zero sends, zero
    approvals, audit row, needs-attention, send row blocked, CRM
    follow-up stays pending for a human."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = 'opted_out', "
            "opt_out_status = 'opted_out' WHERE id = ? AND user_id = ?",
            (lead_id, u1),
        )
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    job = _job_row(scheduled["job_id"])
    assert job["status"] == "succeeded"
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "BLOCK"
    assert result["sent"] is False
    assert _approvals(u1) == []
    blocked = _audit_rows(u1, action="follow_up_sms_blocked")
    assert len(blocked) == 1
    assert blocked[0]["gate_decision"] == "BLOCK"
    assert _needs_attention_rows(u1, lead_id, "follow_up_sms_blocked")
    assert _send_row(scheduled["job_id"])["status"] == "blocked"
    assert _sms_row(scheduled["message_id"], u1)["status"] == "draft"
    assert _follow_up_row(follow_up_id)["status"] == "pending"


def test_suppressed_number_blocks(two_users, monkeypatch):
    """Suppressed number: same BLOCK surface as 2.1, reason suppressed."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with patch("sms_quiet_hours.in_quiet_hours", return_value=False), patch(
        "sms_authorization.in_quiet_hours", return_value=False
    ), patch("sms_authorization._rate_limited", return_value=False), patch(
        "tenant_sms_db.is_suppressed", return_value=True
    ):
        assert run_once() is True
    assert sent_messages() == []
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "BLOCK"
    assert "suppression" in result["reason"]
    assert _approvals(u1) == []
    assert len(_audit_rows(u1, action="follow_up_sms_blocked")) == 1
    assert _send_row(scheduled["job_id"])["status"] == "blocked"


def test_do_not_contact_lead_blocks(two_users, monkeypatch):
    """REGRESSION (criterion 2): leads.status='do_not_contact' BLOCKs
    with no send and no draft. This test FAILS on the old code path —
    neither existing SMS handler passes do_not_contact to the gate,
    so the policy's do-not-contact check was dead for SMS jobs."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET status = 'do_not_contact' WHERE id = ? AND user_id = ?",
            (lead_id, u1),
        )
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "lead is marked do-not-contact"
    assert _approvals(u1) == []
    assert len(_audit_rows(u1, action="follow_up_sms_blocked")) == 1
    assert _send_row(scheduled["job_id"])["status"] == "blocked"


def test_quiet_hours_defers_and_reschedules(two_users, monkeypatch):
    """Quiet hours DEFERs by rescheduling — not dropping, not
    backoff-death: the job settles this firing with rescheduled_for at
    08:00 recipient-local, the send row stays scheduled, and the
    rescheduled firing sends."""
    _mock_env(monkeypatch)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, phone = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with patch("sms_quiet_hours.in_quiet_hours", return_value=True), patch(
        "sms_authorization.in_quiet_hours", return_value=True
    ), patch("sms_authorization._rate_limited", return_value=False), patch(
        "tenant_sms_db.is_suppressed", return_value=False
    ):
        assert run_once() is True
    assert sent_messages() == []
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "DEFER"
    assert result["reason"] == "quiet_hours"
    rescheduled_for = result["rescheduled_for"]
    assert rescheduled_for is not None
    continuation_job_id = result["rescheduled_job_id"]
    assert continuation_job_id is not None
    assert continuation_job_id != scheduled["job_id"]
    job = _job_row(scheduled["job_id"])
    assert job["status"] == "succeeded"  # this firing settled by rescheduling
    continuation = _job_row(continuation_job_id)
    assert continuation["status"] == "pending"
    assert continuation["kind"] == "follow_up_sms_send"
    assert continuation["next_attempt_at"] == rescheduled_for
    send_row = _send_row(continuation_job_id)
    assert send_row is not None
    assert send_row["status"] == "scheduled"
    assert send_row["job_id"] == continuation_job_id
    assert send_row["scheduled_at"] == rescheduled_for
    assert send_row["deferred_count"] == 1
    rescheduled_dt = datetime.fromisoformat(rescheduled_for.replace("Z", "+00:00"))
    assert rescheduled_dt > _now()
    from crm_time import resolve_zone

    assert rescheduled_dt.astimezone(resolve_zone("America/Denver")).hour == 8
    deferred = _audit_rows(u1, action="follow_up_sms_deferred")
    assert len(deferred) == 1
    assert deferred[0]["gate_decision"] == "DEFER"
    assert rescheduled_for in deferred[0]["gate_reason"]
    # The rescheduled firing sends once quiet hours are over.
    _set_job_due(continuation_job_id, _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    assert sent_messages()[0]["body"] == BODY
    assert _send_row(continuation_job_id)["status"] == "sent"


def test_quiet_hours_deferral_cap_dead_letters(two_users, monkeypatch):
    """After 14 nightly deferrals the send dead-letters (job
    dead_letter, send row dead_letter, audit + needs-attention) instead
    of looping forever."""
    _mock_env(monkeypatch)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_follow_up_sends SET deferred_count = 14 WHERE job_id = ?",
            (scheduled["job_id"],),
        )
    with patch("sms_quiet_hours.in_quiet_hours", return_value=True), patch(
        "sms_authorization.in_quiet_hours", return_value=True
    ), patch("sms_authorization._rate_limited", return_value=False), patch(
        "tenant_sms_db.is_suppressed", return_value=False
    ):
        assert run_once() is True
    assert sent_messages() == []
    assert _job_row(scheduled["job_id"])["status"] == "dead_letter"
    assert _send_row(scheduled["job_id"])["status"] == "dead_letter"
    assert _job_result(scheduled["job_id"])["error"] == "quiet_hours_deferral_cap_reached"
    assert len(_audit_rows(u1, action="follow_up_sms_deferred")) == 1
    assert _needs_attention_rows(u1, lead_id, "follow_up_sms_deferred")


def test_rate_limit_defers_retryably(two_users, monkeypatch):
    """Tripped rate limit DEFERs retryably: the job re-queues with
    backoff (pending, future next_attempt_at), zero sends; once the
    limit clears the same job sends."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with patch("sms_quiet_hours.in_quiet_hours", return_value=False), patch(
        "sms_authorization.in_quiet_hours", return_value=False
    ), patch("sms_authorization._rate_limited", return_value=True), patch(
        "tenant_sms_db.is_suppressed", return_value=False
    ):
        assert run_once() is True
    assert sent_messages() == []
    job = _job_row(scheduled["job_id"])
    assert job["status"] == "pending"
    assert job["next_attempt_at"] is not None
    assert datetime.fromisoformat(
        job["next_attempt_at"].replace("Z", "+00:00")
    ) > _now()
    assert len(_audit_rows(u1, action="follow_up_sms_deferred")) == 1
    assert _send_row(scheduled["job_id"])["status"] == "scheduled"
    _set_job_due(scheduled["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    assert _job_result(scheduled["job_id"])["sent"] is True


def test_spend_cap_blocks(two_users, monkeypatch):
    """Ledger past the cap: BLOCK, zero sends, zero approvals, audit
    row, no new cost row; reserve_spend itself refuses at the cap."""
    _mock_env(monkeypatch)
    monkeypatch.setenv("HARNESS_MONTHLY_SPEND_CAP_CENTS", "100")
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    costs.record_cost(
        u1, "anthropic", "seed", 1000, "tokens", cost_cents=5000,
    )
    assert costs.current_month_spend_cents(u1) >= costs.get_monthly_spend_cap_cents()
    assert costs.reserve_spend(u1, 1_000_000, "test-cap-refusal") is False
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)
    assert sent_messages() == []
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "BLOCK"
    assert _approvals(u1) == []
    assert len(_audit_rows(u1, action="follow_up_sms_blocked")) == 1
    assert len(_cost_rows(u1)) == 1  # the seed only; no send cost recorded


def test_missing_consent_evidence_blocks(two_users, monkeypatch):
    """REVOCATION (ruling 10): consent removed after scheduling —
    lead reverts to 'unverified' (no evidence, not a blocked state) —
    BLOCKs at fire time via consent_missing, with no send and no draft."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = 'unverified' "
            "WHERE id = ? AND user_id = ?",
            (lead_id, u1),
        )
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "no messaging consent evidence for this lead/channel"
    assert _approvals(u1) == []
    assert len(_audit_rows(u1, action="follow_up_sms_blocked")) == 1
    assert _send_row(scheduled["job_id"])["status"] == "blocked"


def test_schedule_refuses_lead_without_consent_evidence(two_users, monkeypatch):
    """Schedule-time consent check (ruling 10): an unverified lead
    cannot be scheduled at all — clear error, no job enqueued, no
    sms row, no send row."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, _ = _make_lead(u1, consent_status="unverified")
    with pytest.raises(FollowUpScheduleError) as excinfo:
        _schedule(u1, lead_id)
    assert excinfo.value.code == "consent_missing"
    assert "consent" in str(excinfo.value).lower()
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM harness_jobs").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM harness_follow_up_sends").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sms_messages WHERE user_id = ? AND lead_id = ?",
            (u1, lead_id),
        ).fetchone()["n"] == 0


def test_human_takeover_drafts(two_users, monkeypatch):
    """Human takeover on the conversation: DRAFT + approval, zero sends."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with patch("sms_quiet_hours.in_quiet_hours", return_value=False), patch(
        "sms_authorization.in_quiet_hours", return_value=False
    ), patch("sms_authorization._rate_limited", return_value=False), patch(
        "tenant_sms_db.is_suppressed", return_value=False
    ), patch("harness.inbox.is_human_takeover", return_value=True):
        assert run_once() is True
    assert sent_messages() == []
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "DRAFT"
    assert result["reason"] == "a human has taken over this conversation"
    assert len(_approvals(u1)) == 1


# --- Criterion 3: no double-fire ----------------------------------------------


def test_same_job_claimed_once_under_concurrent_workers(two_users, monkeypatch):
    """One due follow-up job, two workers racing: only one claim
    succeeds and the dispatch sends exactly once."""
    from harness.jobs import claim_jobs
    from workers.harness_worker import dispatch_job

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    first = claim_jobs(limit=10, claimed_by="worker-a")
    assert [j["id"] for j in first] == [scheduled["job_id"]]
    assert claim_jobs(limit=10, claimed_by="worker-b") == []
    with _gate_open():
        assert dispatch_job(first[0]) is True
    assert len(sent_messages()) == 1
    assert _job_row(scheduled["job_id"])["status"] == "succeeded"


def test_double_schedule_enqueues_once(two_users, monkeypatch):
    """Scheduling the same (lead, reason, scheduled minute) twice
    returns the same job id and creates one job, one send row, one
    sms row."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    when = _iso_in(minutes=30)
    first = _schedule(u1, lead_id, follow_up_id, scheduled_at=when)
    second = _schedule(u1, lead_id, follow_up_id, scheduled_at=when)
    assert second["job_id"] == first["job_id"]
    assert second["duplicate"] is True
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM harness_jobs").fetchone()["n"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM harness_follow_up_sends").fetchone()["n"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sms_messages WHERE user_id = ? AND lead_id = ?",
            (u1, lead_id),
        ).fetchone()["n"] == 1


def test_reclaimed_job_after_crash_does_not_resend(two_users, monkeypatch):
    """Layer 4: the send succeeds, then the job is reset to pending
    (crash after provider-accept, before complete_job). The reclaimed
    run returns already_sent, settles, and the mock still shows
    exactly one send."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)
    assert len(sent_messages()) == 1
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET status = 'pending', next_attempt_at = NULL, "
            "claimed_at = NULL, claimed_by = NULL WHERE id = ?",
            (scheduled["job_id"],),
        )
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    result = _job_result(scheduled["job_id"])
    assert result["reason"] == "already_sent"
    assert result["sent"] is False
    assert _job_row(scheduled["job_id"])["status"] == "succeeded"
    assert _send_row(scheduled["job_id"])["status"] == "sent"


def test_same_lead_same_reason_second_send_blocked(two_users, monkeypatch):
    """Lead+reason guard, PM ruling 3, all four limbs:

    (a) in-flight: a second send for the same triple BLOCKs while the
        first is still scheduled;
    (b) within 7 days after a sent row: BLOCKs as duplicate_follow_up_send;
    (c) after 7 days: a repeat may send;
    (d) a different reason for the same lead sends freely.
    """
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, phone = _make_lead(u1)

    # (a) In-flight: schedule A (future, not fired), then schedule B
    # for the same triple and force B due first — B BLOCKs.
    follow_up_a = _make_follow_up(u1, lead_id, reason=REASON)
    sched_a = _schedule(u1, lead_id, follow_up_a, scheduled_at=_iso_in(minutes=60))
    follow_up_b = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=61), reason=REASON
    )
    sched_b = _schedule(
        u1, lead_id, follow_up_b, scheduled_at=_iso_in(minutes=61)
    )
    assert sched_b["job_id"] != sched_a["job_id"]
    _set_job_due(sched_b["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    result_b = _job_result(sched_b["job_id"])
    assert result_b["decision"] == "BLOCK"
    assert result_b["reason"] == "duplicate_follow_up_send"
    assert len(_audit_rows(u1, action="follow_up_sms_blocked")) == 1
    assert _send_row(sched_b["job_id"])["status"] == "blocked"
    # A still sends when its time comes.
    _set_job_due(sched_a["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1

    # (b) Within 7 days after the sent row: a second send BLOCKs.
    follow_up_c = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=62), reason=REASON
    )
    sched_c = _schedule(
        u1, lead_id, follow_up_c, scheduled_at=_iso_in(minutes=62)
    )
    _set_job_due(sched_c["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    result_c = _job_result(sched_c["job_id"])
    assert result_c["decision"] == "BLOCK"
    assert result_c["reason"] == "duplicate_follow_up_send"
    assert len(_audit_rows(u1, action="follow_up_sms_blocked")) == 2

    # (c) After 7 days: the same triple may send again. Age the sent
    # row past the window (the blocked rows do not count).
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_follow_up_sends SET sent_at = ? WHERE job_id = ?",
            ((_now() - timedelta(days=8)).isoformat(), sched_a["job_id"]),
        )
    follow_up_d = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=63), reason=REASON
    )
    sched_d = _schedule(
        u1, lead_id, follow_up_d, scheduled_at=_iso_in(minutes=63)
    )
    _set_job_due(sched_d["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 2
    assert _job_result(sched_d["job_id"])["sent"] is True

    # (d) A different reason for the same lead sends freely, even
    # inside the window.
    follow_up_e = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=64), reason=REASON_OTHER
    )
    sched_e = _schedule(
        u1, lead_id, follow_up_e, scheduled_at=_iso_in(minutes=64),
        reason=REASON_OTHER,
    )
    _set_job_due(sched_e["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 3
    assert _job_result(sched_e["job_id"])["sent"] is True


def test_approved_twice_executes_once(two_users, monkeypatch):
    """Approving the same follow_up_sms approval twice: the second
    approve is refused (not_pending), exactly one execute_approval
    job exists (execute-approval-<id> idempotency), one mock send."""
    from harness.approvals import approve_approval, enqueue_execute_approval_job

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="drafts_only", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)
    approval_id = _job_result(scheduled["job_id"])["approval_id"]

    approval, error = approve_approval(u1, approval_id)
    assert error is None, error
    _, error2 = approve_approval(u1, approval_id)
    assert error2 == "not_pending"
    job_a = enqueue_execute_approval_job(u1, approval_id)
    job_b = enqueue_execute_approval_job(u1, approval_id)
    assert job_a == job_b
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM harness_jobs WHERE kind = 'execute_approval'"
        ).fetchone()["n"] == 1
    clear_sent_messages()
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    assert _approval_row(approval_id)["status"] == "executed"


def test_retry_after_transient_provider_error_sends_once(two_users, monkeypatch):
    """Transient provider error: first run is retryable (job pending
    with backoff, sms row failed/sending, no provider id recorded as
    sent); the retry sends exactly once — one mock send total."""
    from sms_providers.base import SmsProviderError
    from sms_providers.mock import MockSMSProvider

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)

    original = MockSMSProvider.send_message
    calls = {"n": 0}

    def _flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SmsProviderError("Mock transient failure", provider="mock")
        return original(self, **kwargs)

    with patch.object(MockSMSProvider, "send_message", _flaky):
        with _gate_open():
            assert run_once() is True
    assert sent_messages() == []
    job = _job_row(scheduled["job_id"])
    assert job["status"] == "pending"
    assert job["next_attempt_at"] is not None
    assert datetime.fromisoformat(
        job["next_attempt_at"].replace("Z", "+00:00")
    ) > _now()
    sms = _sms_row(scheduled["message_id"], u1)
    assert sms["status"] in ("sending", "failed")
    assert not sms["provider_message_id"]
    assert _send_row(scheduled["job_id"])["status"] == "scheduled"

    _set_job_due(scheduled["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    assert sent_messages()[0]["body"] == BODY
    assert _job_result(scheduled["job_id"])["sent"] is True


# --- Criterion 4: inbox/lead records show the send ----------------------------


def test_sent_follow_up_visible_in_lead_timeline(two_users, monkeypatch):
    """After the 1.2 send: the outbound row is in the lead's message
    lists, a lead-activity row carries the message id, and the CRM
    follow-up is done with completed_at."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)

    listed = db.list_sms_messages(u1, lead_id=lead_id)
    assert any(m["id"] == scheduled["message_id"] for m in listed)
    assert any(m["status"] in ("sent", "queued", "mock_sent") for m in listed)
    lead_messages = db.list_lead_messages(u1, lead_id)
    assert any(m["id"] == scheduled["message_id"] for m in lead_messages)

    import crm_db

    activities = crm_db.list_lead_activities(u1, lead_id)
    sent_activities = [a for a in activities if a["event_type"] == "follow_up_sms_sent"]
    assert len(sent_activities) == 1
    payload = json.loads(sent_activities[0]["payload_json"])
    assert payload["message_id"] == scheduled["message_id"]
    assert payload["follow_up_id"] == follow_up_id

    row = _follow_up_row(follow_up_id)
    assert row["status"] == "done"
    assert row["completed_at"] is not None


def test_blocked_follow_up_visible_in_audit_and_attention(two_users, monkeypatch):
    """After the 2.1 block: the audit row and the needs-attention row
    exist, no sms row flipped to sent, and the CRM follow-up is still
    pending for a human to act on."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = 'opted_out', "
            "opt_out_status = 'opted_out' WHERE id = ? AND user_id = ?",
            (lead_id, u1),
        )
    with _gate_open():
        assert run_once() is True
    assert len(_audit_rows(u1, action="follow_up_sms_blocked")) == 1
    assert _needs_attention_rows(u1, lead_id, "follow_up_sms_blocked")
    assert _sms_row(scheduled["message_id"], u1)["status"] == "draft"
    assert _follow_up_row(follow_up_id)["status"] == "pending"


def test_drafted_follow_up_visible_in_pending_approvals(two_users, monkeypatch):
    """After the 1.3 draft: list_pending_approvals returns the row with
    the exact draft text and the gate reason."""
    from harness.approvals import list_pending_approvals

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="drafts_only", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _run_due(u1, lead_id, follow_up_id)
    pending = list_pending_approvals(u1)
    assert len(pending) == 1
    item = pending[0]
    assert item["kind"] == "follow_up_sms"
    assert item["draft_reply"] == BODY
    assert item["lead_id"] == lead_id
    assert item["gate_reason"] == "mode_drafts_only"
    assert item["draft"]["message_id"] == scheduled["message_id"]


# --- Scheduling surface: caps, past times, Ask TopAI action -------------------


def test_schedule_enforces_pending_caps(two_users, monkeypatch):
    """Ruling 6: max 5 pending sends per lead and max 50 per tenant,
    enforced at schedule time with a clear error and nothing enqueued
    for the refused call."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, _ = _make_lead(u1)
    for i in range(5):
        _schedule(u1, lead_id, scheduled_at=_iso_in(minutes=10 + i),
                  reason=f"cap check {i}")
    with pytest.raises(FollowUpScheduleError) as excinfo:
        _schedule(u1, lead_id, scheduled_at=_iso_in(minutes=99),
                  reason="cap check overflow")
    assert excinfo.value.code == "per_lead_cap_reached"
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM harness_follow_up_sends WHERE user_id = ?",
            (u1,),
        ).fetchone()["n"] == 5
    # Fill the tenant to 50 (9 more leads x 5), then the 51st refuses.
    for _ in range(9):
        other_lead, _ = _make_lead(u1)
        for i in range(5):
            _schedule(u1, other_lead,
                      scheduled_at=_iso_in(minutes=10 + i),
                      reason=f"tenant cap {other_lead} {i}")
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM harness_follow_up_sends WHERE user_id = ?",
            (u1,),
        ).fetchone()["n"] == 50
    fresh_lead, _ = _make_lead(u1)
    with pytest.raises(FollowUpScheduleError) as excinfo:
        _schedule(u1, fresh_lead, scheduled_at=_iso_in(minutes=99))
    assert excinfo.value.code == "pending_cap_reached"


def test_past_scheduled_at_clamps_and_reports_effective_time(two_users, monkeypatch):
    """A past scheduled_at does not silently fire later: the returned
    effective fire time is now (clamped_to_now), and the job is due
    immediately."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    result = _schedule(u1, lead_id, scheduled_at=_iso_in(minutes=-30))
    assert result["clamped_to_now"] is True
    effective = datetime.fromisoformat(
        result["scheduled_at"].replace("Z", "+00:00")
    )
    assert effective <= _now() + timedelta(seconds=5)
    assert effective >= _now() - timedelta(seconds=60)
    assert _job_row(result["job_id"])["next_attempt_at"] == result["scheduled_at"]
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1


def test_ask_topai_schedule_follow_up_sms_action(two_users, monkeypatch):
    """The Ask TopAI action schedules through the same helper (job id
    + effective fire time returned), surfaces validation errors, and
    is registered as a write tool with a model-facing schema."""
    from ask_topai import registry
    from ask_topai.actions import execute_command

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, _ = _make_lead(u1)
    assert "schedule_follow_up_sms" in registry.WRITE_TOOLS
    names = {item["name"] for item in registry.anthropic_tools()}
    assert "schedule_follow_up_sms" in names
    assert "schedule_follow_up_sms" in {
        item["name"] for item in registry.openai_tools()
    }

    action, payload, error, _choices = execute_command(
        u1,
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_id": lead_id,
                "body": BODY,
                "scheduled_at": _iso_in(minutes=15),
                "reason": REASON,
            },
        },
        None,
    )
    assert action == "schedule_follow_up_sms"
    assert error is None, error
    assert payload["job_id"] is not None
    assert payload["scheduled_at"] is not None
    assert _job_row(payload["job_id"])["kind"] == "follow_up_sms_send"

    # A lead without consent evidence: the action returns the error,
    # nothing is enqueued for it.
    bad_lead, _ = _make_lead(u1, consent_status="unverified")
    _action, _payload, error, _choices = execute_command(
        u1,
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_id": bad_lead,
                "body": BODY,
                "scheduled_at": _iso_in(minutes=15),
                "reason": REASON,
            },
        },
        None,
    )
    assert error is not None
    assert "consent" in error.lower()


# --- Scheduling-fix regressions (2026-11-22) -------------------------
#
# Live staging verification (2026-11-22) hit "Ask TopAI cannot do
# that yet." on all three scheduling attempts: ``schedule_follow_up_sms``
# was registered in ask_topai/registry.py WRITE_TOOLS + the model tool
# schema, but NOT in ask_topai/schemas.py ALLOWED_ACTIONS, so
# ``sanitize_command`` (and ``parser.validate_model_payload``) rejected
# it before the handler ran. The 31 tests above call ``execute_command``
# directly, bypassing sanitize/parser — that is why the suite was green
# while the live path failed. These tests drive the action through
# ``sanitize_command`` and the parser entry point instead.


def test_sanitize_schedule_follow_up_sms_keeps_arguments():
    """sanitize_command passes the action with body, scheduled_at,
    reason, and the lead resolution fields intact and verbatim."""
    from ask_topai.schemas import ALLOWED_ACTIONS, sanitize_command

    assert "schedule_follow_up_sms" in ALLOWED_ACTIONS
    scheduled_at = _iso_in(minutes=15)
    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_name": "Follow-up Lead",
                "body": BODY,
                "scheduled_at": scheduled_at,
                "reason": REASON,
            },
        },
        "Schedule a follow-up SMS to Follow-up Lead",
    )
    assert err is None, err
    assert cleaned is not None
    assert cleaned["action"] == "schedule_follow_up_sms"
    args = cleaned["arguments"]
    assert args["lead_name"] == "Follow-up Lead"
    assert args["body"] == BODY
    assert args["scheduled_at"] == scheduled_at
    assert args["reason"] == REASON


def test_sanitize_schedule_follow_up_sms_aliases():
    """The alias shapes the model actually sends (lead_id vs lead_name,
    due_at vs scheduled_at, message/text vs body) all survive the
    sanitize step, normalized to the keys the handler reads."""
    from ask_topai.schemas import sanitize_command

    scheduled_at = _iso_in(minutes=30)
    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_id": 42,
                "message": BODY,
                "due_at": scheduled_at,
                "reason": REASON,
                "follow_up_id": 7,
            },
        },
        "Text the lead in 30 minutes",
    )
    assert err is None, err
    args = cleaned["arguments"]
    assert args["lead_id"] == 42
    assert args["body"] == BODY
    assert args["scheduled_at"] == scheduled_at
    assert args["reason"] == REASON
    assert args["follow_up_id"] == 7
    assert "message" not in args
    assert "due_at" not in args

    # text alias, and the reason defaults to "Follow up" when absent.
    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_name": "Follow-up Lead",
                "text": BODY,
                "scheduled_at": scheduled_at,
            },
        },
        "Text the lead",
    )
    assert err is None, err
    assert cleaned["arguments"]["body"] == BODY
    assert cleaned["arguments"]["scheduled_at"] == scheduled_at
    assert cleaned["arguments"]["reason"] == "Follow up"
    assert "text" not in cleaned["arguments"]


def test_parser_accepts_schedule_follow_up_sms():
    """The same command passes the parser entry point
    (``validate_model_payload``), which gates on ALLOWED_ACTIONS
    before calling sanitize_command — the live path's first gate."""
    from ask_topai.parser import validate_model_payload

    scheduled_at = _iso_in(minutes=15)
    result = validate_model_payload(
        {
            "status": "ok",
            "message": "Scheduling the follow-up SMS.",
            "commands": [
                {
                    "action": "schedule_follow_up_sms",
                    "arguments": {
                        "lead_name": "Follow-up Lead",
                        "body": BODY,
                        "scheduled_at": scheduled_at,
                        "reason": REASON,
                    },
                }
            ],
        },
        "Schedule a follow-up SMS to Follow-up Lead",
    )
    assert result["status"] == "ok", result
    assert len(result["commands"]) == 1
    command = result["commands"][0]
    assert command["action"] == "schedule_follow_up_sms"
    assert command["arguments"]["lead_name"] == "Follow-up Lead"
    assert command["arguments"]["body"] == BODY
    assert command["arguments"]["scheduled_at"] == scheduled_at
    assert command["arguments"]["reason"] == REASON


def test_sanitize_then_execute_schedule_follow_up_sms(two_users, monkeypatch):
    """The sanitized command executes end to end: sanitize_command
    output feeds execute_command and enqueues the follow-up job —
    the two halves of the live path joined, not just each in isolation."""
    from ask_topai.actions import execute_command
    from ask_topai.schemas import sanitize_command

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, _ = _make_lead(u1)
    scheduled_at = _iso_in(minutes=15)
    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_id": lead_id,
                "body": BODY,
                "scheduled_at": scheduled_at,
                "reason": REASON,
            },
        },
        "Schedule a follow-up SMS",
    )
    assert err is None, err
    action, payload, error, _choices = execute_command(u1, cleaned, None)
    assert action == "schedule_follow_up_sms"
    assert error is None, error
    assert payload["job_id"] is not None
    assert _job_row(payload["job_id"])["kind"] == "follow_up_sms_send"


def test_sanitize_unknown_action_still_refused():
    """The allowlist gate still refuses unknown actions, through both
    sanitize_command and the parser, with the same message the live
    path showed for the (then-missing) scheduling action."""
    from ask_topai.parser import validate_model_payload
    from ask_topai.schemas import sanitize_command

    cleaned, err = sanitize_command(
        {"action": "send_sms_now", "arguments": {"body": "hi"}},
        "Send an SMS now",
    )
    assert cleaned is None
    assert err == "Ask TopAI cannot do that yet."

    result = validate_model_payload(
        {
            "status": "ok",
            "message": "",
            "commands": [{"action": "teleport_lead", "arguments": {}}],
        },
        "Teleport the lead",
    )
    assert result["status"] == "unsupported"
    assert result["commands"] == []
    assert "Ask TopAI cannot do that yet." in result["message"]


def test_sanitize_schedule_follow_up_sms_missing_fields(two_users, monkeypatch):
    """Missing scheduled_at and missing body each produce a clear
    error and schedule nothing — no job, no send row."""
    from ask_topai.actions import execute_command
    from ask_topai.schemas import sanitize_command

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, _ = _make_lead(u1)

    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {"lead_id": lead_id, "body": BODY, "reason": REASON},
        },
        "Schedule a follow-up SMS",
    )
    assert err == "I need a time to send the follow-up SMS."
    _action, _payload, error, _choices = execute_command(u1, cleaned, None)
    assert error is not None
    assert _send_rows_for_lead(u1, lead_id) == []

    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_id": lead_id,
                "scheduled_at": _iso_in(minutes=15),
                "reason": REASON,
            },
        },
        "Schedule a follow-up SMS",
    )
    assert err == "Message body is required."
    _action, _payload, error, _choices = execute_command(u1, cleaned, None)
    assert error is not None
    assert _send_rows_for_lead(u1, lead_id) == []
