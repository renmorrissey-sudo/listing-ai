"""Acceptance re-test: re-executes the previously FAILING assertions from
the 2026-11-08 Phase I Production Acceptance Report against the fixed code.

The original acceptance files (tests/test_acceptance_t1.py, t3t4, t5, t6)
lived only in disposable audit copies that no longer exist, so the failing
assertions are reconstructed here from the report + evidence files
(hidden_files/acceptance/*.md). Each test name cites the deficiency it
re-tests (C1, C2, H1–H4, D2, D3, M1/M2). Two Medium deficiencies from the
original audit (M4 failure alerting, D5 invalid-args coercion) were
explicitly out of the fix scope and are NOT asserted here — they are
recorded as still open in the acceptance report addendum.

All providers are mocked; nothing here sends a real SMS/email/call.
"""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import db
from harness import costs as harness_costs
from harness import jobs as harness_jobs
from harness import tools as harness_tools
from harness.policy import MODES, decide


def _set_mode(user_id, mode="drafts_only", paused=False, ai_enabled=True):
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
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, name="Retest Lead"):
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": name, "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    return lead_id, phone


def _voice_call(user_id):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, lead_id, provider, direction, lead_name,
                 phone_number, status, provider_call_id, created_at)
            VALUES (?, NULL, NULL, 'vapi', 'outbound', 'Voice Tools',
                    '+13035550199', 'started', ?, ?)
            """,
            (user_id, "vapi_tools", now),
        )
        return cur.lastrowid


def _vapi_payload(call_id, tool_name, arguments):
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": "vapi_tools", "metadata": {"topai_call_id": str(call_id)}},
            "toolCallList": [
                {"id": "tool_1", "name": tool_name, "arguments": arguments}
            ],
        }
    }


def _audit_actions(user_id):
    from harness.audit import list_audit

    return [(r["action"], r["gate_decision"]) for r in list_audit(user_id)]


# --- H1: policy fails closed on unknown tools --------------------------------

def test_retest_h1_unknown_tool_blocks_in_all_modes(two_users):
    u1, _ = two_users
    for mode in MODES:
        decision, reason = decide(u1, "frobnicate_tool_xyz", "frobnicate_tool_xyz", {"mode": mode})
        assert decision == "BLOCK", (mode, reason)


# --- H2: registry and policy agree on sends ----------------------------------

def test_retest_h2_registry_policy_agree_for_every_tool(two_users):
    u1, _ = two_users
    for entry in harness_tools.list_tools():
        name, risk = entry["name"], entry["risk_class"]
        decisions = {
            decide(u1, name, name, {"mode": mode})[0] for mode in MODES
        }
        if risk == "restricted":
            assert decisions == {"BLOCK"}, (name, decisions)
        else:
            # No routine/sensitive tool may BLOCK in every mode.
            assert decisions != {"BLOCK"}, (name, decisions)


# --- C1: Vapi gated ------------------------------------------------------------

def test_retest_c1_vapi_consent_change_blocked(two_users):
    import voice_tools

    u1, _ = two_users
    db.init_db()
    lead_id, _phone = _make_lead(u1)
    call_id = _voice_call(u1)
    payload = _vapi_payload(
        call_id,
        "update_lead_sms_consent_status",
        {"lead_id": lead_id, "sms_consent_status": "verified"},
    )
    result = voice_tools.handle_vapi_tool_calls(payload)
    assert isinstance(result["results"][0]["result"], str)
    assert db.get_lead(lead_id, u1)["sms_consent_status"] != "verified"
    assert any(
        action == "vapi_tool_call" and gate == "BLOCK"
        for action, gate in _audit_actions(u1)
    )


def test_retest_c1_vapi_send_email_blocked(two_users):
    import voice_tools

    u1, _ = two_users
    db.init_db()
    lead_id, _phone = _make_lead(u1)
    call_id = _voice_call(u1)
    payload = _vapi_payload(
        call_id,
        "send_lead_email",
        {"lead_id": lead_id, "subject": "Hi", "body": "Hello"},
    )
    with patch("voice_tools.send_lead_email") as mock_send:
        result = voice_tools.handle_vapi_tool_calls(payload)
    assert not mock_send.called
    assert isinstance(result["results"][0]["result"], str)


def test_retest_c1_vapi_closed_won_blocked_in_authorized(two_users):
    import voice_tools

    u1, _ = two_users
    db.init_db()
    _set_mode(u1, "authorized")
    lead_id, _phone = _make_lead(u1)
    call_id = _voice_call(u1)
    payload = _vapi_payload(
        call_id, "update_lead_status", {"lead_id": lead_id, "status": "closed_won"}
    )
    voice_tools.handle_vapi_tool_calls(payload)
    assert db.get_lead(lead_id, u1)["status"] != "closed_won"


# --- H3: redaction at enqueue ---------------------------------------------------

def test_retest_h3_no_secrets_in_stored_job_payload(two_users):
    u1, _ = two_users
    job_id = harness_jobs.enqueue_job(
        u1,
        "sms_auto_reply",
        {"lead_id": 1, "inbound_id": 2, "body": "my key is sk-ant-api03-FAKESECRET1234567890 call +13035550142"},
        idempotency_key="retest-h3-1",
    )
    with db.get_db() as conn:
        row = dict(
            conn.execute(
                "SELECT payload_json FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )
    assert "FAKESECRET" not in row["payload_json"]
    assert "sk-ant-api03" not in row["payload_json"]
    harness_jobs.complete_job(job_id)


# --- H4: scheduled follow-up kind exists -----------------------------------------

def test_retest_h4_scheduled_follow_up_executes(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    db.init_db()
    lead_id, _phone = _make_lead(u1)
    job_id = harness_jobs.enqueue_job(
        u1,
        "scheduled_follow_up",
        {"lead_id": lead_id, "note": "Call about the listing"},
        idempotency_key="retest-h4-1",
    )
    assert run_once() is True
    with db.get_db() as conn:
        job = dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )
        followups = conn.execute(
            "SELECT * FROM lead_follow_ups WHERE user_id = ? AND lead_id = ?",
            (u1, lead_id),
        ).fetchall()
    assert job["status"] == "succeeded"
    assert len(followups) >= 1


# --- C2: rate limits gate the SMS path --------------------------------------------

def test_retest_c2_rate_limited_job_defers_and_requeues(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    db.init_db()
    _set_mode(u1, "authorized")
    lead_id, phone = _make_lead(u1)
    inbound_id = db.create_sms_message(
        u1, None, "telnyx",
        {"phone_number": phone, "message_body": "Hi, is it available?"},
        status="received", lead_id=lead_id, direction="inbound",
    )
    for _ in range(30):
        db.create_sms_message(
            u1, None, "telnyx",
            {"phone_number": phone, "message_body": "outbound"},
            status="sent", lead_id=lead_id, direction="outbound",
        )
    job_id = harness_jobs.enqueue_job(
        u1, "sms_auto_reply",
        {"lead_id": lead_id, "inbound_id": inbound_id},
        idempotency_key="retest-c2-1",
    )
    coach_result = {
        "analysis": {
            "summary": "Availability question.",
            "intent": "question",
            "draft_reply": "Hi! Yes, it is still available.",
            "suggested_reply": "Hi! Yes, it is still available.",
            "sensitive_topic": False,
            "requires_manual_review": False,
            "escalation_topics": [],
            "suggested_lead_status": "contacted",
        }
    }
    with patch(
        "sms_inbound.analyze_inbound_and_coach", return_value=coach_result
    ), patch("sms_ai_agent.process_inbound_ai") as mock_send:
        assert run_once() is True
        assert not mock_send.called
    with db.get_db() as conn:
        job = dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )
    assert job["status"] == "pending"  # re-queued, not completed/dead
    assert job["next_attempt_at"]


# --- D2: spending cap -------------------------------------------------------------

def test_retest_d2_spending_cap_blocks_sends(two_users):
    u1, _ = two_users
    harness_costs.record_cost(u1, "anthropic", "send_sms", 10_000_000, "tokens", cost_cents=5_000_000)
    decision, reason = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "BLOCK", reason
    decision, _reason = decide(u1, "find_lead", "find_lead", {"mode": "authorized"})
    assert decision == "ALLOW"


# --- D3: billing mutations / customer delete restricted ----------------------------

def test_retest_d3_subscription_and_customer_delete_blocked(two_users):
    u1, _ = two_users
    for name in (
        "change_subscription",
        "modify_subscription",
        "update_subscription",
        "delete_customer",
    ):
        for mode in MODES:
            decision, reason = decide(u1, name, name, {"mode": mode})
            assert decision == "BLOCK", (name, mode, reason)


# --- M1/M2: approval flow works end-to-end ------------------------------------------

def test_retest_m1m2_approve_execute_once_via_route_and_worker(app_client, two_users):
    import json as _json
    from datetime import datetime, timezone

    u1, _ = two_users
    db.init_db()
    lead_id, phone = _make_lead(u1)
    inbound_id = db.create_sms_message(
        u1, None, "telnyx",
        {"phone_number": phone, "message_body": "Hi, is it available?"},
        status="received", lead_id=lead_id, direction="inbound",
    )
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_approvals
                (user_id, job_id, kind, draft_json, status, created_at)
            VALUES (?, NULL, 'sms_reply', ?, 'pending', ?)
            """,
            (
                u1,
                _json.dumps(
                    {"draft_reply": "Hi! Yes.", "lead_id": lead_id, "inbound_id": inbound_id}
                ),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        approval_id = cur.lastrowid
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
        user = db.get_user_by_id(u1)
        sess["session_version"] = int((user or {}).get("session_version") or 1)
    resp = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    from workers.harness_worker import run_once

    with patch(
        "sms_quiet_hours.in_quiet_hours", return_value=False
    ), patch("sms_authorization._rate_limited", return_value=False), patch(
        "tenant_sms_db.is_suppressed", return_value=False
    ), patch("sms_ai_agent.process_inbound_ai") as mock_send:
        mock_send.return_value = {"replied": True, "message_id": "m1"}
        assert run_once() is True
        assert mock_send.call_count == 1
        assert run_once() in (True, False)
        assert mock_send.call_count == 1  # second pass cannot re-send
    with db.get_db() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM harness_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        )
    assert row["status"] == "executed"


# --- Previously-passing regression: drafts-only e2e trace ------------------------------

def test_retest_drafts_only_sms_job_drafts_and_never_sends(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    db.init_db()
    lead_id, phone = _make_lead(u1)
    inbound_id = db.create_sms_message(
        u1, None, "telnyx",
        {"phone_number": phone, "message_body": "Hi, is it available?"},
        status="received", lead_id=lead_id, direction="inbound",
    )
    job_id = harness_jobs.enqueue_job(
        u1, "sms_auto_reply",
        {"lead_id": lead_id, "inbound_id": inbound_id},
        idempotency_key="retest-e2e-1",
    )
    coach_result = {
        "analysis": {
            "summary": "Availability question.",
            "intent": "question",
            "draft_reply": "Hi! Yes, it is still available.",
            "suggested_reply": "Hi! Yes, it is still available.",
            "sensitive_topic": False,
            "requires_manual_review": False,
            "escalation_topics": [],
            "suggested_lead_status": "contacted",
        }
    }
    with patch(
        "sms_inbound.analyze_inbound_and_coach", return_value=coach_result
    ), patch("sms_ai_agent.process_inbound_ai") as mock_send:
        assert run_once() is True
        assert not mock_send.called
    with db.get_db() as conn:
        job = dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )
        approvals = conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ? AND status = 'pending'",
            (u1,),
        ).fetchall()
    assert job["status"] == "succeeded"
    assert len(approvals) == 1
