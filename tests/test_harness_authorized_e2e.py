"""Authorized-mode worker-level end-to-end tests (Phase II inventory item 2).

The gate-level authorized tests (tests/test_harness_policy_gate.py)
prove ``policy.decide`` ALLOWs a routine send in authorized mode, but
no job-level test ran the authorized path: the ``sms_auto_reply`` job
through ``workers.harness_worker.run_once`` — coach -> policy gate ->
real ``process_inbound_ai`` send path -> mock provider — in a tenant
whose stored ``harness_settings`` mode is ``authorized``.

These tests close that gap. Every test enqueues a real
``sms_auto_reply`` job and runs it through the worker (``run_once``),
then asserts on the durable outcome: job status/result, the mock
provider's recorded sends, the outbound ``sms_messages`` row, and the
``harness_approvals`` rows.

Setup notes (all following existing harness-test precedent):

* NOT staging: ``IS_STAGING`` stays False, so the stored ``authorized``
  mode is honoured (the staging tests prove the opposite lock).
* ``HARNESS_MOCK_PROVIDERS`` is on via ``_staging_mock_config`` — the
  mock provider records the send and never touches the network, and
  the Telnyx toll-free/trial delivery checks are skipped for mock
  sends. No real SMS is ever sent.
* The coach (Anthropic) is mocked with a canned analysis, except in
  the spend-cap test, which deliberately runs the REAL
  ``analyze_inbound_and_coach`` so the upstream
  ``reserve_spend`` refusal is exercised, not bypassed.
* ``_GateOpen`` pins quiet hours / rate limit / suppression open so
  results do not depend on the time of day.

Deterministic only: no model, no provider, no network.
"""

import json
import uuid
from unittest.mock import patch

import pytest

import db
from harness import costs
from harness.jobs import enqueue_job
from sms_providers.mock import clear_sent_messages, sent_messages
from tests.test_harness_staging_safety import (
    _GateOpen,
    _set_settings,
    _staging_mock_config,
)
from workers.harness_worker import run_once

DRAFT_REPLY = "Hi! Yes, it is still available. Want to tour Saturday?"


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py).
    Clear the harness queue and the mock provider's recorded sends so
    each test's worker run sees only its own job/send."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    clear_sent_messages()
    yield


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, phone=None, consent_status="verified",
               opt_out_status="active"):
    """A lead with explicit consent state (set directly — the upsert
    helper does not carry sms_consent_status through)."""
    from lead_service import upsert_crm_lead

    phone = phone or _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Authorized E2E Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = ?, opt_out_status = ? "
            "WHERE id = ? AND user_id = ?",
            (consent_status, opt_out_status, lead_id, user_id),
        )
    return lead_id, phone


def _make_inbound(user_id, lead_id, phone, body):
    return db.create_sms_message(
        user_id, None, "telnyx",
        {"phone_number": phone, "message_body": body},
        status="received", lead_id=lead_id, direction="inbound",
    )


def _routine_analysis(**overrides):
    analysis = {
        "summary": "Lead asked about availability.",
        "intent": "question",
        "draft_reply": DRAFT_REPLY,
        "suggested_reply": DRAFT_REPLY,
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "suggested_lead_status": "contacted",
        "appointment_details": None,
        "appointment_requested": False,
    }
    analysis.update(overrides)
    return analysis


def _coach_result(analysis):
    return {"analysis": analysis, "insight_id": None, "error": None}


def _run_job(user_id, lead_id, inbound_id, body):
    """Enqueue one sms_auto_reply job and run a single worker pass."""
    job_id = enqueue_job(
        user_id,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": body,
            "receiving_number": "+18880000000",
        },
        idempotency_key=f"auth-e2e-{uuid.uuid4().hex[:12]}",
    )
    assert run_once() is True, "worker ran nothing for the enqueued job"
    return job_id


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


def _approvals(user_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_approvals WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        ]


def _outbound_rows(user_id, lead_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sms_messages WHERE user_id = ? AND lead_id = ? "
                "AND direction = 'outbound'",
                (user_id, lead_id),
            ).fetchall()
        ]


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


def _set_authorized(user_id):
    _set_settings(user_id, mode="authorized", paused=False)


# --- Positive: authorized routine reply actually sends -----------------------


def test_authorized_worker_sends_routine_reply_via_mock_provider(
    two_users, monkeypatch
):
    """Authorized tenant + consenting, non-suppressed lead + routine
    inbound: the worker runs the job to ALLOW, the real
    ``process_inbound_ai`` send path fires, and exactly one mock send
    is recorded with the coach's draft text — with an outbound
    ``sms_messages`` row and NO approval row."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_authorized(u1)
    lead_id, phone = _make_lead(u1)
    inbound_id = _make_inbound(
        u1, lead_id, phone, "Hi, is the house still available?"
    )
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value=_coach_result(_routine_analysis()),
    ):
        job_id = _run_job(
            u1, lead_id, inbound_id, "Hi, is the house still available?"
        )

    job = _job_row(job_id)
    assert job["status"] == "succeeded", job
    result = _job_result(job_id)
    assert result.get("decision") == "ALLOW", result
    assert result.get("sent") is True, result
    assert result.get("approval_id") is None

    recorded = sent_messages()
    assert len(recorded) == 1, recorded
    assert recorded[0]["to"] == phone
    assert recorded[0]["body"] == DRAFT_REPLY
    assert recorded[0]["status"] == "mock_sent"
    assert recorded[0]["provider_message_id"].startswith("mock-")

    outbound = _outbound_rows(u1, lead_id)
    assert len(outbound) == 1, outbound
    assert outbound[0]["message_body"] == DRAFT_REPLY
    assert (outbound[0].get("provider_message_id") or "").startswith("mock-")
    assert outbound[0]["reply_to_message_id"] == inbound_id

    assert _approvals(u1) == []
    sent_audit = _audit_rows(u1, action="sms_auto_reply_sent")
    assert len(sent_audit) == 1
    assert sent_audit[0]["gate_decision"] == "ALLOW"


# --- Negative: escalation topic drafts, never sends --------------------------


def test_authorized_worker_drafts_escalation_topic_and_sends_nothing(
    two_users, monkeypatch
):
    """Inbound text whose coach analysis flags an escalation topic
    (``legal``, from ``autonomy.SMS_ESCALATION_BLOCK``) must draft for
    a human in authorized mode too: a pending approval row carrying
    the topic, a DRAFT decision, and zero sends."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_authorized(u1)
    lead_id, phone = _make_lead(u1)
    inbound_id = _make_inbound(
        u1, lead_id, phone,
        "I want to talk to a lawyer about this contract.",
    )
    escalation = _routine_analysis(
        summary="Lead threatens legal action over the contract.",
        intent="complaint",
        sensitive_topic=True,
        requires_manual_review=True,
        escalation_topics=["legal"],
    )
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value=_coach_result(escalation),
    ):
        job_id = _run_job(
            u1, lead_id, inbound_id,
            "I want to talk to a lawyer about this contract.",
        )

    job = _job_row(job_id)
    assert job["status"] == "succeeded", job
    result = _job_result(job_id)
    assert result.get("decision") == "DRAFT", result
    assert result.get("sent") is False, result
    assert "escalation" in (result.get("reason") or "").lower(), result
    assert result.get("approval_id") is not None

    approvals = _approvals(u1)
    assert len(approvals) == 1, approvals
    assert approvals[0]["id"] == result["approval_id"]
    assert approvals[0]["status"] == "pending"
    assert approvals[0]["kind"] == "sms_reply"
    draft = json.loads(approvals[0]["draft_json"])
    assert draft["draft_reply"] == DRAFT_REPLY
    assert "legal" in (draft.get("escalation_topics") or [])

    assert sent_messages() == []
    assert _outbound_rows(u1, lead_id) == []
    drafted = _audit_rows(u1, action="sms_auto_reply_drafted")
    assert len(drafted) == 1
    assert drafted[0]["gate_decision"] == "DRAFT"


# --- Negative: at/above the monthly spend cap blocks, never sends ------------


def test_authorized_worker_blocks_at_spend_cap_and_sends_nothing(
    two_users, monkeypatch
):
    """Tenant at/above the monthly spend cap (ledger seeded far past
    the $50 default cap): the REAL coach entry point refuses the
    reservation, so the job BLOCKs with ``spend_cap_reached`` — no
    model call, no send, no approval. The worker dead-letters the job
    (the handler returns ok=False, retryable=False for a cap trip,
    since the cap only resets monthly)."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_authorized(u1)
    # Seed the ledger so the cap logic itself is exercised (same shape
    # as the gate-level cap test): $50,000 of usage vs a $50 cap.
    costs.record_cost(
        u1, "anthropic", "send_sms", 10_000_000, "tokens",
        cost_cents=5_000_000,
    )
    assert costs.current_month_spend_cents(u1) >= costs.get_monthly_spend_cap_cents()

    lead_id, phone = _make_lead(u1)
    inbound_id = _make_inbound(
        u1, lead_id, phone, "Hi, is the house still available?"
    )
    # No coach patch on purpose: the real analyze_inbound_and_coach
    # must hit the reserve_spend refusal before any model call.
    with _GateOpen():
        job_id = _run_job(
            u1, lead_id, inbound_id, "Hi, is the house still available?"
        )

    job = _job_row(job_id)
    assert job["status"] == "dead_letter", job
    result = _job_result(job_id)
    assert result.get("error") == "spend_cap_reached", result

    blocked = _audit_rows(u1, action="sms_auto_reply_coach_failed")
    assert len(blocked) == 1, blocked
    assert blocked[0]["gate_decision"] == "BLOCK"
    assert blocked[0]["gate_reason"] == "spend_cap_reached"

    assert sent_messages() == []
    assert _outbound_rows(u1, lead_id) == []
    assert _approvals(u1) == []


# --- Negative: opted-out lead blocks, never sends -----------------------------


def test_authorized_worker_blocks_opted_out_lead_and_sends_nothing(
    two_users, monkeypatch
):
    """Lead with consent ``opted_out`` / opt-out status ``opted_out``:
    the policy gate BLOCKs in authorized mode — no send, no approval,
    even though the coach produced a routine draft."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_authorized(u1)
    lead_id, phone = _make_lead(
        u1, consent_status="opted_out", opt_out_status="opted_out"
    )
    inbound_id = _make_inbound(
        u1, lead_id, phone, "Hi, is the house still available?"
    )
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value=_coach_result(_routine_analysis()),
    ):
        job_id = _run_job(
            u1, lead_id, inbound_id, "Hi, is the house still available?"
        )

    job = _job_row(job_id)
    assert job["status"] == "succeeded", job
    result = _job_result(job_id)
    assert result.get("decision") == "BLOCK", result
    assert result.get("sent") is False, result
    assert result.get("approval_id") is None
    assert "opt" in (result.get("reason") or "").lower(), result

    blocked = _audit_rows(u1, action="sms_auto_reply_blocked")
    assert len(blocked) == 1, blocked
    assert blocked[0]["gate_decision"] == "BLOCK"

    assert sent_messages() == []
    assert _outbound_rows(u1, lead_id) == []
    assert _approvals(u1) == []
