"""Phase 1 harness tenant-isolation tests.

Two tenants (the two_users fixture) must never see each other's jobs,
audit rows, costs, memory, approvals, or inbox conversations, and the
orchestrator running for user A must never touch user B's leads.
"""

import json
import uuid
from unittest.mock import patch

import db
from harness import audit, costs, inbox, jobs, memory
from harness.memory import get_memory, lead_summary, load_profile, set_memory


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, name="Isolation Lead"):
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, lead = upsert_crm_lead(
        user_id, phone, {"name": name, "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    return lead_id, phone


def _make_sms_message(user_id, lead_id, phone, body="Hello from the lead"):
    return db.create_sms_message(
        user_id,
        None,
        "telnyx",
        {"lead_name": "Isolation Lead", "phone_number": phone, "message_body": body},
        status="sent",
        lead_id=lead_id,
        direction="inbound",
    )


def test_jobs_are_tenant_isolated(two_users):
    u1, u2 = two_users
    job_id = jobs.enqueue_job(u1, "sms_auto_reply", {"owner": "a"})
    assert jobs.get_job(job_id, u1) is not None
    assert jobs.get_job(job_id, u2) is None
    assert jobs.list_jobs(u2) == []
    assert [j["id"] for j in jobs.list_jobs(u1)] == [job_id]
    jobs.complete_job(job_id)


def test_audit_is_tenant_isolated(two_users):
    u1, u2 = two_users
    audit.record_audit(u1, "ai", "secret-action", gate_decision="DRAFT")
    assert audit.list_audit(u2) == []
    assert len(audit.list_audit(u1)) == 1


def test_costs_are_tenant_isolated(two_users):
    u1, u2 = two_users
    costs.record_cost(u1, "telnyx", "send_sms", 1, "segments", cost_cents=5)
    costs.record_anthropic_usage(
        u1, "sms_auto_reply",
        {"input_tokens": 1_000_000, "output_tokens": 0},
        model="claude-sonnet-5",
    )
    period = costs.list_costs(u1)[0]["period"]
    assert costs.list_costs(u2) == []
    assert costs.monthly_totals(u2, period)["total_cents"] == 0
    assert costs.monthly_totals(u2, period)["by_provider"] == {}
    totals = costs.monthly_totals(u1, period)
    # 5c Telnyx + 300c for 1M Sonnet input tokens (placeholder pricing).
    assert totals["total_cents"] == 305
    assert totals["by_provider"]["telnyx"] == 5
    assert totals["by_provider"]["anthropic"] == 300
    assert totals["by_tool"]["send_sms"] == 5


def test_memory_is_tenant_isolated(two_users):
    u1, u2 = two_users
    assert set_memory(u1, "preferred_name", "Ren") is True
    assert get_memory(u1, "preferred_name") == "Ren"
    assert get_memory(u2, "preferred_name") is None
    assert set_memory(u2, "preferred_name", "Other") is True
    assert get_memory(u1, "preferred_name") == "Ren"
    assert get_memory(u2, "preferred_name") == "Other"
    # Overwriting one tenant's key never touches the other's.
    assert set_memory(u1, "preferred_name", {"first": "Ren"}) is True
    assert get_memory(u1, "preferred_name") == {"first": "Ren"}
    assert get_memory(u2, "preferred_name") == "Other"
    assert get_memory(u1, "no_such_key") is None


def test_approvals_are_tenant_isolated(two_users):
    u1, u2 = two_users
    job_id = jobs.enqueue_job(u1, "sms_auto_reply", {"owner": "a"})
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_approvals (user_id, job_id, kind, draft_json, status, created_at)
            VALUES (?, ?, 'sms_reply', ?, 'pending', '2026-01-01T00:00:00+00:00')
            """,
            (u1, job_id, json.dumps({"draft_reply": "Hi!"})),
        )
        approval_id = cur.lastrowid
    with db.get_db() as conn:
        rows_b = conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ?", (u2,)
        ).fetchall()
        rows_a = conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ?", (u1,)
        ).fetchall()
    assert rows_b == []
    assert [dict(r)["id"] for r in rows_a] == [approval_id]
    jobs.complete_job(job_id)


def test_inbox_is_tenant_isolated(two_users):
    u1, u2 = two_users
    lead_id, phone = _make_lead(u1)
    _make_sms_message(u1, lead_id, phone, "Hi, I am interested in the house.")
    convos_b = inbox.list_sms_conversations(u2)
    assert convos_b == []
    assert inbox.get_conversation(u2, lead_id) is None
    convos_a = inbox.list_sms_conversations(u1)
    assert len(convos_a) == 1
    assert convos_a[0]["lead_id"] == lead_id
    assert convos_a[0]["message_count"] == 1
    assert "interested" in convos_a[0]["last_message_preview"]
    thread = inbox.get_conversation(u1, lead_id)
    assert thread is not None
    assert thread["lead"]["lead_id"] == lead_id
    assert len(thread["messages"]) == 1
    assert thread["messages"][0]["message_body"] == "Hi, I am interested in the house."


def test_cross_tenant_lead_summary_returns_none(two_users):
    u1, u2 = two_users
    lead_id, _ = _make_lead(u1)
    assert lead_summary(u2, lead_id) is None
    summary = lead_summary(u1, lead_id)
    assert summary is not None
    assert summary["lead_id"] == lead_id
    assert summary["name"] == "Isolation Lead"
    # Summaries only: no raw message bodies leak through.
    assert "message_body" not in json.dumps(summary)
    assert lead_summary(u1, 999999999) is None


def test_load_profile_is_tenant_scoped(two_users):
    u1, u2 = two_users
    profile_a = load_profile(u1)
    profile_b = load_profile(u2)
    assert profile_a.get("email") != profile_b.get("email")
    assert load_profile(999999999) == {}


def test_orchestrator_run_for_user_a_never_touches_user_b_leads(two_users):
    from harness import orchestrator

    u1, u2 = two_users
    lead_b, _ = _make_lead(u2, name="User B Lead")
    before = db.get_lead(lead_b, u2)
    assert before["status"] != "closed_won"
    fake_result = {
        "status": "ok",
        "message": "I will update the lead.",
        "commands": [
            {
                "action": "update_lead_status",
                "arguments": {"lead_id": lead_b, "status": "closed_won"},
            }
        ],
        "tools_invoked": ["update_lead_status"],
        "model": "claude-sonnet-5",
    }
    with patch("ask_topai.agent.complete", return_value=fake_result) as mock_complete:
        result = orchestrator.run(u1, "mark that lead closed won", {"lead_id": lead_b})
    assert mock_complete.called
    # D5: the proposed command references another tenant's lead, so the
    # orchestrator's validation rejects it outright (BLOCK) before the
    # policy gate — it never reaches DRAFT, and nothing executes.
    assert result["gate_decision"] == "BLOCK"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["decision"] == "BLOCK"
    assert "cross_tenant_lead" in result["tool_calls"][0]["reason"]
    assert result["audit_id"] is not None
    after = db.get_lead(lead_b, u2)
    assert after["status"] == before["status"]
    # The audit row belongs to user A, never to user B.
    assert audit.list_audit(u2) == []
    assert any(
        r["id"] == result["audit_id"] for r in audit.list_audit(u1)
    )
