"""POST /sms/test gated through the harness (Phase II Batch 1, item 3).

The endpoint no longer sends inline: it validates, creates a draft
``sms_messages`` row and enqueues one ``manual_sms_send`` job, returning
202. The worker re-checks the harness gate (mode, consent, suppression,
quiet hours, rate limits, spend cap) immediately before any send; in
Drafts Only or paused the job drafts and sends nothing.

All provider sends are mocked (``config.HARNESS_MOCK_PROVIDERS``) and
asserted through ``sms_providers.mock.sent_messages`` — nothing here
sends a real SMS. Gate inputs that depend on wall-clock time (quiet
hours) or live counters are patched to their open state unless the
test is about that input.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import config
import db
import tenant_sms_db as tdb
from harness import costs
from harness.jobs import enqueue_job
from harness.policy import set_mode, set_paused
from sms_providers.mock import clear_sent_messages, sent_messages
from workers.harness_worker import run_once


@pytest.fixture(autouse=True)
def _isolate_job_queue_and_mock():
    """Clear the shared job queue and the in-memory mock sends so the
    worker picks up this test's job, not a leftover (see
    tests/test_harness_approvals.py)."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    clear_sent_messages()
    yield
    clear_sent_messages()


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, opted_out=False):
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Manual Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    if opted_out:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE leads SET sms_consent_status = 'opted_out',"
                " opt_out_status = 'opted_out', sms_sending_blocked = 1"
                " WHERE id = ? AND user_id = ?",
                (lead_id, user_id),
            )
    return lead_id, phone


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


def _mock_env(monkeypatch):
    """Mock provider for every send; simpletexting so the Telnyx
    toll-free and trial gates pass without Telnyx credentials."""
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")


@contextmanager
def _gate_open():
    """Force the gate inputs open (no quiet hours, no rate limit, not
    suppressed) so results do not depend on when the suite runs.

    Quiet hours needs TWO patch targets: the harness gate reads
    ``sms_quiet_hours.in_quiet_hours`` via a lazy import (so patching
    the module attribute works), but ``can_send_sms`` — reached on the
    ALLOW path through ``send_authorized_sms`` — consults the name
    ``sms_authorization`` bound at ITS import time (``from
    sms_quiet_hours import in_quiet_hours``). Patching only the module
    attribute leaves the send path enforcing real quiet hours whenever
    ``sms_authorization`` was imported before the patch context opens
    (e.g. by an earlier test importing the app), which dead-letters
    the job after 21:00 local. Patch both lookup sites.
    """
    with patch("sms_quiet_hours.in_quiet_hours", return_value=False), patch(
        "sms_authorization.in_quiet_hours", return_value=False
    ), patch("sms_authorization._rate_limited", return_value=False), patch(
        "tenant_sms_db.is_suppressed", return_value=False
    ):
        yield


def _make_message_row(user_id, to_number, body, lead_id=None):
    return db.create_sms_message(
        user_id=user_id,
        persona_id=None,
        provider=config.SMS_PROVIDER,
        data={
            "lead_name": "Manual Lead",
            "phone_number": to_number,
            "lead_type": "buyer",
            "message_body": body,
            "desired_outcome": "verify SMS connectivity",
        },
        status="draft",
        lead_id=lead_id,
        direction="outbound",
        consent_status="confirmed" if lead_id else "unknown",
        opt_out_status="active",
    )


def _enqueue_manual(user_id, message_id, to_number, lead_id=None, compliance=True):
    return enqueue_job(
        user_id,
        "manual_sms_send",
        {
            "message_id": message_id,
            "lead_id": lead_id,
            "to": to_number,
            "compliance_confirmed": compliance,
            "source_page": "sms_test",
        },
        idempotency_key=f"manual-sms-test-{message_id}-{uuid.uuid4().hex[:6]}",
        correlation_id=f"sms-test:{message_id}",
    )


def _job_row(job_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def _audit_actions(user_id):
    from harness.audit import list_audit

    return [r["action"] for r in list_audit(user_id)]


def _message_row(message_id, user_id):
    return db.get_sms_message(message_id, user_id)


# --- Endpoint: enqueue only, nothing sends inline ---------------------------


def test_endpoint_enqueues_and_sends_nothing_inline(
    app_client, two_users, monkeypatch
):
    _mock_env(monkeypatch)
    u1, _ = two_users
    lead_id, phone = _make_lead(u1)
    _login(app_client, u1)
    resp = app_client.post(
        "/sms/test",
        json={"to": phone, "message": "Hi, test ping", "compliance_confirmed": True},
    )
    assert resp.status_code == 202, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True and body["queued"] is True
    assert body["job_id"]
    job = _job_row(body["job_id"])
    assert job is not None and job["kind"] == "manual_sms_send"
    assert job["status"] == "pending"
    # The endpoint itself sent nothing — the send is the worker's job.
    assert sent_messages() == []


def test_endpoint_requires_compliance_for_lead(app_client, two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _, phone = _make_lead(u1)
    _login(app_client, u1)
    resp = app_client.post(
        "/sms/test", json={"to": phone, "message": "Hi, test ping"}
    )
    assert resp.status_code == 400
    assert "consent" in (resp.get_json() or {}).get("error", "").lower()


def test_endpoint_trial_blocks_non_lead_when_telnyx_trial(
    app_client, two_users, monkeypatch
):
    _mock_env(monkeypatch)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "TELNYX_VERIFIED_TEST_NUMBER", "")
    monkeypatch.setattr(
        "sms_authorization.check_telnyx_toll_free_send_allowed",
        lambda: (True, None),
    )
    u1, _ = two_users
    _login(app_client, u1)
    resp = app_client.post(
        "/sms/test",
        json={"to": "+15551230099", "message": "Hi, test ping"},
    )
    assert resp.status_code == 403
    assert "trial" in (resp.get_json() or {}).get("error", "").lower()


def test_endpoint_double_submit_enqueues_once(app_client, two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    lead_id, phone = _make_lead(u1)
    _login(app_client, u1)
    payload = {"to": phone, "message": "Hi, double submit", "compliance_confirmed": True}
    first = app_client.post("/sms/test", json=payload)
    second = app_client.post("/sms/test", json=payload)
    assert first.status_code == 202 and second.status_code == 202
    assert first.get_json()["job_id"] == second.get_json()["job_id"]
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM harness_jobs WHERE user_id = ? AND kind = 'manual_sms_send'",
            (u1,),
        ).fetchall()
    assert len(rows) == 1


# --- Worker: authorized mode sends (lead + non-lead) ------------------------


def test_authorized_lead_send_via_worker(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, phone = _make_lead(u1)
    set_mode(u1, "authorized")
    message_id = _make_message_row(u1, phone, "Hi, this is a test send", lead_id)
    _enqueue_manual(u1, message_id, phone, lead_id)
    with _gate_open():
        assert run_once() is True
    sends = sent_messages()
    assert len(sends) == 1
    assert sends[0]["to"] == phone
    assert sends[0]["body"] == "Hi, this is a test send"
    assert sends[0]["status"] == "mock_sent"
    row = _message_row(message_id, u1)
    assert row["status"] == "mock_sent"
    assert row["provider_message_id"] == sends[0]["provider_message_id"]
    assert "manual_sms_sent" in _audit_actions(u1)


def test_authorized_non_lead_send_via_worker(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    set_mode(u1, "authorized")
    to_number = _unique_phone("555")
    message_id = _make_message_row(u1, to_number, "Hi, non-lead test")
    _enqueue_manual(u1, message_id, to_number)
    with _gate_open():
        assert run_once() is True
    sends = sent_messages()
    assert len(sends) == 1
    assert sends[0]["to"] == to_number
    assert _message_row(message_id, u1)["status"] == "mock_sent"
    assert "manual_sms_sent" in _audit_actions(u1)


# --- Worker: drafts_only / paused draft, never send -------------------------


def test_drafts_only_job_drafts_no_send(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, phone = _make_lead(u1)
    settings, err = set_mode(u1, "drafts_only")
    assert err is None and settings["mode"] == "drafts_only"
    message_id = _make_message_row(u1, phone, "Hi, drafts only", lead_id)
    _enqueue_manual(u1, message_id, phone, lead_id)
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    assert _message_row(message_id, u1)["status"] == "draft"
    assert "manual_sms_drafted" in _audit_actions(u1)
    assert "manual_sms_sent" not in _audit_actions(u1)


def test_paused_job_drafts_no_send(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, phone = _make_lead(u1)
    set_mode(u1, "authorized")
    set_paused(u1, True)
    message_id = _make_message_row(u1, phone, "Hi, paused tenant", lead_id)
    _enqueue_manual(u1, message_id, phone, lead_id)
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    assert _message_row(message_id, u1)["status"] == "draft"
    assert "manual_sms_drafted" in _audit_actions(u1)


# --- Worker: gate blocks ----------------------------------------------------


def test_opted_out_lead_blocks(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, phone = _make_lead(u1, opted_out=True)
    set_mode(u1, "authorized")
    message_id = _make_message_row(u1, phone, "Hi, opted out", lead_id)
    _enqueue_manual(u1, message_id, phone, lead_id)
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    assert "manual_sms_blocked" in _audit_actions(u1)
    assert "manual_sms_sent" not in _audit_actions(u1)


def test_spend_cap_blocks(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, phone = _make_lead(u1)
    set_mode(u1, "authorized")
    costs.record_cost(
        u1, "anthropic", "send_sms", 10_000_000, "tokens", cost_cents=5_000_000
    )
    message_id = _make_message_row(u1, phone, "Hi, over cap", lead_id)
    _enqueue_manual(u1, message_id, phone, lead_id)
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    assert "manual_sms_blocked" in _audit_actions(u1)
    assert "manual_sms_sent" not in _audit_actions(u1)


def test_quiet_hours_defers(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, phone = _make_lead(u1)
    set_mode(u1, "authorized")
    message_id = _make_message_row(u1, phone, "Hi, quiet hours", lead_id)
    _enqueue_manual(u1, message_id, phone, lead_id)
    with patch("sms_quiet_hours.in_quiet_hours", return_value=True), patch(
        "sms_authorization._rate_limited", return_value=False
    ), patch("tenant_sms_db.is_suppressed", return_value=False):
        assert run_once() is True
    assert sent_messages() == []
    assert "manual_sms_deferred" in _audit_actions(u1)
    assert "manual_sms_sent" not in _audit_actions(u1)


# --- Worker: malformed jobs fail closed -------------------------------------


def test_job_with_missing_message_fails_closed(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    set_mode(u1, "authorized")
    job_id = enqueue_job(
        u1,
        "manual_sms_send",
        {"message_id": 999999999, "to": "+15551230099", "compliance_confirmed": True},
        idempotency_key=f"manual-sms-missing-{uuid.uuid4().hex[:6]}",
    )
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    # A job pointing at a message row that does not exist is malformed:
    # it sends nothing and the worker dead-letters it (terminal,
    # fail-closed — it is never retried or silently dropped).
    assert _job_row(job_id)["status"] == "dead_letter"


def test_job_without_compliance_blocks_lead_send(two_users, monkeypatch):
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    lead_id, phone = _make_lead(u1)
    set_mode(u1, "authorized")
    message_id = _make_message_row(u1, phone, "Hi, no compliance flag", lead_id)
    _enqueue_manual(u1, message_id, phone, lead_id, compliance=False)
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    assert "manual_sms_blocked" in _audit_actions(u1)
