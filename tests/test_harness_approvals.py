"""Human-approval flow tests (harness/approvals.py + routes + worker).

Covers M1/M2 from the TopAI AI Harness acceptance test: approve/reject/
execute end-to-end through the durable ``execute_approval`` job, tenant
isolation (404, never reveal), rejection enqueuing nothing, 7-day expiry,
execution-time policy re-check (opt-out blocks the send), the status page,
and double-approve / double-execute idempotency.

All provider sends are mocked (``sms_ai_agent.process_inbound_ai``);
nothing here sends a real SMS. Gate inputs that depend on wall-clock
time (quiet hours) or live counters are patched to their open state in
the execute tests so results do not depend on when the suite runs.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import db


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py), so
    earlier tests can leave harness jobs behind. The execute tests in
    this file drive the worker (run_once), which claims the oldest
    pending job, so clear the queue first: the job the worker picks up
    is then this test's job, not a leftover."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    yield


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id):
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Approval Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    return lead_id


def _insert_approval(user_id, lead_id, inbound_id, draft_reply="Hi! Yes, it is still available.",
                     created_at=None):
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_approvals
                (user_id, job_id, kind, draft_json, status, created_at)
            VALUES (?, ?, 'sms_reply', ?, 'pending', ?)
            """,
            (
                user_id,
                None,
                json.dumps(
                    {
                        "draft_reply": draft_reply,
                        "lead_id": lead_id,
                        "inbound_id": inbound_id,
                    }
                ),
                created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def _approval(approval_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return dict(row) if row else None


def _audit_actions(user_id):
    from harness.audit import list_audit

    return [(r["action"], r["actor"]) for r in list_audit(user_id)]


def _execute_jobs(user_id):
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM harness_jobs WHERE user_id = ? AND kind = 'execute_approval'",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@contextmanager
def _gate_open():
    """Force the execution-time gate inputs open (no quiet hours, no rate
    limit, not suppressed) so execute tests are time-independent."""
    with patch("sms_quiet_hours.in_quiet_hours", return_value=False), patch(
        "sms_authorization._rate_limited", return_value=False
    ), patch("tenant_sms_db.is_suppressed", return_value=False):
        yield


# --- 1. Approve -> execute end-to-end (real sms_handler DRAFT path) ---------


def test_approve_execute_end_to_end(app_client, two_users):
    """Drive the real sms_handler DRAFT path (mocked coach) to create the
    approval, approve it via the route, then run the worker: the mocked
    send path is called exactly once, the approval ends executed, and
    audit rows exist for approve + execute."""
    from harness.jobs import enqueue_job
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id = _make_lead(u1)
    inbound_id = 424242
    job_id = enqueue_job(
        u1,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": "Hi, is the house still available?",
            "receiving_number": "+18880000000",
        },
        idempotency_key=f"sms-auto-reply-e2e-{inbound_id}-{uuid.uuid4().hex[:6]}",
    )
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
    with patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": analysis, "insight_id": None, "error": None},
    ), patch("sms_ai_agent.process_inbound_ai") as mock_send:
        assert run_once() is True
        assert not mock_send.called  # DRAFT path never sends
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ? AND status = 'pending'",
            (u1,),
        ).fetchone()
    assert row is not None
    approval_id = dict(row)["id"]
    assert _approval(approval_id)["status"] == "pending"

    _login(app_client, u1)
    resp = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert resp.status_code == 200
    assert _approval(approval_id)["status"] == "approved"
    jobs = _execute_jobs(u1)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "pending"

    with _gate_open(), patch(
        "sms_ai_agent.process_inbound_ai",
        return_value={"replied": True, "reason": "sent", "message_id": "msg-1"},
    ) as mock_send:
        assert run_once() is True
        assert mock_send.call_count == 1
        args, _kwargs = mock_send.call_args
        assert args[0] == u1 and args[1] == lead_id and args[2] == inbound_id

    assert _approval(approval_id)["status"] == "executed"
    assert jobs[0]["id"] is not None
    with db.get_db() as conn:
        job_row = dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (jobs[0]["id"],)
            ).fetchone()
        )
    assert job_row["status"] == "succeeded"
    actions = _audit_actions(u1)
    assert ("approval_approved", "user") in actions
    assert ("approval_executed", "system") in actions


# --- 2. Cross-tenant ----------------------------------------------------------


def test_cross_tenant_approve_is_404(app_client, two_users):
    from workers.harness_worker import run_once

    u1, u2 = two_users
    lead_id = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424243)
    _login(app_client, u2)
    resp = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert resp.status_code == 404
    assert _approval(approval_id)["status"] == "pending"
    assert _execute_jobs(u2) == []
    assert _execute_jobs(u1) == []
    # The other tenant's approval is invisible in the list, too.
    assert app_client.get("/api/harness/approvals").get_json() == {"approvals": []}
    with patch("sms_ai_agent.process_inbound_ai") as mock_send:
        run_once()
        assert not mock_send.called


# --- 3. Reject -----------------------------------------------------------------


def test_reject_enqueues_nothing_and_never_sends(app_client, two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424244)
    _login(app_client, u1)
    resp = app_client.post(
        f"/api/harness/approvals/{approval_id}/reject",
        json={"reason": "wrong tone"},
    )
    assert resp.status_code == 200
    assert _approval(approval_id)["status"] == "rejected"
    assert _execute_jobs(u1) == []
    with _gate_open(), patch(
        "sms_ai_agent.process_inbound_ai",
        return_value={"replied": True, "reason": "sent"},
    ) as mock_send:
        run_once()
        assert not mock_send.called
    assert _approval(approval_id)["status"] == "rejected"
    actions = _audit_actions(u1)
    assert ("approval_rejected", "user") in actions


# --- 4. Expiry ------------------------------------------------------------------


def test_expired_approval_cannot_be_approved(app_client, two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id = _make_lead(u1)
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    approval_id = _insert_approval(u1, lead_id, 424245, created_at=eight_days_ago)
    _login(app_client, u1)
    resp = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert resp.status_code in (400, 409, 410)
    assert _approval(approval_id)["status"] == "expired"
    assert _execute_jobs(u1) == []
    with patch("sms_ai_agent.process_inbound_ai") as mock_send:
        run_once()
        assert not mock_send.called
    assert ("approval_expired", "system") in _audit_actions(u1)


# --- 5. Execution-time re-check --------------------------------------------------


def test_execution_blocked_when_lead_opts_out_after_approval(app_client, two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424246)
    _login(app_client, u1)
    resp = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert resp.status_code == 200
    # The lead opts out between approval and execution.
    db.mark_lead_opt_out(lead_id, u1)
    with _gate_open(), patch(
        "sms_ai_agent.process_inbound_ai",
        return_value={"replied": True, "reason": "sent"},
    ) as mock_send:
        assert run_once() is True
        assert not mock_send.called
    assert _approval(approval_id)["status"] == "blocked"
    actions = _audit_actions(u1)
    assert ("approval_blocked", "system") in actions


# --- 6. Status page --------------------------------------------------------------


def test_status_page_renders_and_requires_login(app_client, two_users):
    u1, _ = two_users
    lead_id = _make_lead(u1)
    draft = "Hi! Yes, it is still available. Want to tour Saturday?"
    approval_id = _insert_approval(u1, lead_id, 424247, draft_reply=draft)
    # Anonymous: redirected to login (login_required), not a 200 page.
    resp = app_client.get("/harness")
    assert resp.status_code in (302, 401)
    if resp.status_code == 302:
        assert "/login" in resp.headers.get("Location", "")
    _login(app_client, u1)
    resp = app_client.get("/harness")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "drafts_only" in html or "Drafts Only" in html
    assert draft in html
    assert str(approval_id) in html
    assert "Approve" in html and "Reject" in html
    # The nav link is present on the page.
    assert 'href="/harness"' in html
    # The JSON list endpoint is user-scoped and shows the pending approval.
    listed = app_client.get("/api/harness/approvals").get_json()["approvals"]
    assert [a["id"] for a in listed] == [approval_id]
    assert listed[0]["draft_reply"] == draft
    assert listed[0]["expires_at"]


# --- 7. Double-approve / double-execute -------------------------------------------


def test_double_approve_and_double_execute_send_at_most_once(app_client, two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    lead_id = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424248)
    _login(app_client, u1)
    first = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert first.status_code == 200
    # Approving twice is an error and creates no second job.
    second = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert second.status_code == 409
    assert len(_execute_jobs(u1)) == 1
    with _gate_open(), patch(
        "sms_ai_agent.process_inbound_ai",
        return_value={"replied": True, "reason": "sent", "message_id": "msg-2"},
    ) as mock_send:
        assert run_once() is True
        assert mock_send.call_count == 1
        # Running the worker again must not send a second time.
        run_once()
        assert mock_send.call_count == 1
    assert _approval(approval_id)["status"] == "executed"


def test_execute_pending_approval_directly_sends_nothing(two_users):
    """Defence in depth: execute_approved on a never-approved row does
    not send, even if a job for it somehow exists."""
    from harness.approvals import execute_approved

    u1, _ = two_users
    lead_id = _make_lead(u1)
    approval_id = _insert_approval(u1, lead_id, 424249)
    with patch("sms_ai_agent.process_inbound_ai") as mock_send:
        result = execute_approved(u1, approval_id)
        assert not mock_send.called
    assert result["ok"] is True
    assert result["sent"] is False
    assert _approval(approval_id)["status"] == "pending"
