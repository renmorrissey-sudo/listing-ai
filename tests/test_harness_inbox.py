"""Unified inbox tests (Phase II Priority 2, M2).

Covers the backlog acceptance criteria:
1. Tenant isolation — another tenant's leads never appear.
2. Search/filter by lead name/phone + unread filter, against live data.
3. Pending approvals in the inbox with Approve/Reject; approving
   executes the stored draft exactly.
4. Takeover flag: subsequent inbound drafts, no auto-send, even in
   Authorized mode; clearing resumes normal gating. Audited.
5. (Suite-level: full suite green; staging deploy + live browser
   verification are the parent's job, not this file's.)

Plus the PM's exact-text requirement: the inbox shows the approval's
stored draft text verbatim — one consistent version from the single
stored draft row, never a derived or cached copy.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import db
from harness import inbox as inbox_mod
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
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    clear_sent_messages()
    yield


@contextmanager
def _login_as(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)
    yield


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, name="Inbox Lead", phone=None):
    from lead_service import upsert_crm_lead

    phone = phone or _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": name, "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = ?, opt_out_status = ? "
            "WHERE id = ? AND user_id = ?",
            ("verified", "active", lead_id, user_id),
        )
    return lead_id, phone


def _make_inbound(user_id, lead_id, phone, body):
    return db.create_sms_message(
        user_id, None, "telnyx",
        {"phone_number": phone, "message_body": body},
        status="received", lead_id=lead_id, direction="inbound",
    )


def _make_outbound(user_id, lead_id, phone, body):
    return db.create_sms_message(
        user_id, None, "telnyx",
        {"phone_number": phone, "message_body": body},
        status="sent", lead_id=lead_id, direction="outbound",
    )


def _insert_approval(user_id, lead_id, inbound_id, draft_reply=DRAFT_REPLY):
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
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def _audit_actions(user_id):
    from harness.audit import list_audit

    return [(r["action"], r["actor"]) for r in list_audit(user_id)]


def _approvals(user_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_approvals WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        ]


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


def _run_auto_reply_job(user_id, lead_id, inbound_id, body):
    job_id = enqueue_job(
        user_id,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": body,
            "receiving_number": "+18880000000",
        },
        idempotency_key=f"inbox-takeover-{uuid.uuid4().hex[:12]}",
    )
    assert run_once() is True, "worker ran nothing for the enqueued job"
    with db.get_db() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )
    raw = row.get("result_json")
    return json.loads(raw) if raw else {}


# --- Criterion 1: tenant isolation -------------------------------------------


def test_inbox_lists_only_signed_in_tenants_conversations(two_users):
    u1, u2 = two_users
    lead1, phone1 = _make_lead(u1, name="Owner One")
    _make_inbound(u1, lead1, phone1, "Hello from owner one")
    lead2, phone2 = _make_lead(u2, name="Owner Two")
    _make_inbound(u2, lead2, phone2, "Hello from owner two")

    convs_u1 = inbox_mod.list_sms_conversations(u1)
    assert [c["lead_id"] for c in convs_u1] == [lead1]
    assert convs_u1[0]["lead_name"] == "Owner One"
    convs_u2 = inbox_mod.list_sms_conversations(u2)
    assert [c["lead_id"] for c in convs_u2] == [lead2]


def test_inbox_conversation_and_state_404_for_other_tenant(
    app_client, two_users
):
    u1, u2 = two_users
    lead1, phone1 = _make_lead(u1, name="Owner One")
    _make_inbound(u1, lead1, phone1, "Hello from owner one")

    # Module level: another tenant's conversation is invisible.
    assert inbox_mod.get_conversation(u2, lead1) is None
    assert inbox_mod.set_human_takeover(u2, lead1, True) is None
    assert inbox_mod.mark_conversation_read(u2, lead1) is None

    # API level: 404, never revealing the conversation exists.
    with _login_as(app_client, u2):
        assert app_client.get(
            f"/api/harness/inbox/conversations/{lead1}"
        ).status_code == 404
        assert app_client.post(
            f"/api/harness/inbox/conversations/{lead1}/takeover",
            json={"active": True},
        ).status_code == 404
        assert app_client.post(
            f"/api/harness/inbox/conversations/{lead1}/read"
        ).status_code == 404


# --- Criterion 2: search/filter + unread -------------------------------------


def test_inbox_search_by_name_and_phone(two_users):
    u1, _ = two_users
    lead_a, phone_a = _make_lead(u1, name="Alice Hartman")
    _make_inbound(u1, lead_a, phone_a, "Hi Alice here")
    lead_b, phone_b = _make_lead(u1, name="Bob Keene")
    _make_inbound(u1, lead_b, phone_b, "Hi Bob here")

    by_name = inbox_mod.list_sms_conversations(u1, query="hartman")
    assert [c["lead_id"] for c in by_name] == [lead_a]
    by_phone = inbox_mod.list_sms_conversations(u1, query=phone_b[-4:])
    assert [c["lead_id"] for c in by_phone] == [lead_b]
    no_match = inbox_mod.list_sms_conversations(u1, query="zzz-no-such-name")
    assert no_match == []
    all_convs = inbox_mod.list_sms_conversations(u1)
    assert {c["lead_id"] for c in all_convs} == {lead_a, lead_b}


def test_inbox_unread_filter_and_mark_read(two_users):
    u1, _ = two_users
    lead_a, phone_a = _make_lead(u1, name="Unread Lead")
    _make_inbound(u1, lead_a, phone_a, "First inbound")
    _make_inbound(u1, lead_a, phone_a, "Second inbound")
    lead_b, phone_b = _make_lead(u1, name="Read Lead")
    _make_inbound(u1, lead_b, phone_b, "Only inbound")
    # Reading B clears its unread badge; A stays unread.
    inbox_mod.mark_conversation_read(u1, lead_b)

    convs = inbox_mod.list_sms_conversations(u1)
    by_id = {c["lead_id"]: c for c in convs}
    assert by_id[lead_a]["unread_count"] == 2
    assert by_id[lead_b]["unread_count"] == 0
    unread_only = inbox_mod.list_sms_conversations(u1, unread_only=True)
    assert [c["lead_id"] for c in unread_only] == [lead_a]

    # An outbound-only reply does not count as unread.
    _make_outbound(u1, lead_a, phone_a, "Agent replied")
    convs = inbox_mod.list_sms_conversations(u1)
    by_id = {c["lead_id"]: c for c in convs}
    assert by_id[lead_a]["unread_count"] == 2


def test_inbox_page_search_and_unread_filter(app_client, two_users):
    u1, _ = two_users
    lead_a, phone_a = _make_lead(u1, name="Page Search Lead")
    _make_inbound(u1, lead_a, phone_a, "Searchable inbound")
    lead_b, phone_b = _make_lead(u1, name="Other Lead")
    _make_inbound(u1, lead_b, phone_b, "Other inbound")
    inbox_mod.mark_conversation_read(u1, lead_b)

    with _login_as(app_client, u1):
        page = app_client.get("/harness/inbox")
        assert page.status_code == 200
        assert b"Page Search Lead" in page.data
        assert b"Other Lead" in page.data

        page = app_client.get("/harness/inbox?q=page%20search")
        assert page.status_code == 200
        assert b"Page Search Lead" in page.data
        assert b"Other Lead" not in page.data

        page = app_client.get("/harness/inbox?unread=1")
        assert page.status_code == 200
        assert b"Page Search Lead" in page.data
        assert b"Other Lead" not in page.data


# --- Criterion 3: approvals in the inbox, exact stored draft -----------------


def test_inbox_shows_exact_stored_draft_text(app_client, two_users):
    """The inbox renders the approval's stored draft_reply verbatim.

    The stored draft is the single source of truth: the page must show
    exactly what the approval row stores, and must not show a different
    (derived/cached) copy. A decoy string that resembles a draft must
    not appear.
    """
    u1, _ = two_users
    lead_id, phone = _make_lead(u1, name="Draft Lead")
    inbound_id = _make_inbound(u1, lead_id, phone, "Is the house available?")
    stored = (
        "Hi! Yes — the 3-bed on Maple is still available. "
        "Want to tour Saturday at 11am? — Ren"
    )
    decoy = "Hi there — thank you for reaching out! We have a great home."
    _insert_approval(u1, lead_id, inbound_id, draft_reply=stored)

    with _login_as(app_client, u1):
        page = app_client.get("/harness/inbox")
        assert page.status_code == 200
        assert stored.encode() in page.data
        assert decoy.encode() not in page.data

    # The API surface agrees with the page: same stored text.
    from harness.approvals import list_pending_approvals

    pending = list_pending_approvals(u1)
    assert len(pending) == 1
    assert pending[0]["draft_reply"] == stored
    assert pending[0]["lead_id"] == lead_id


def test_inbox_approve_executes_stored_draft_exactly(
    app_client, two_users, monkeypatch
):
    """Approving in the inbox executes the stored draft (fidelity).

    Mirrors the Nov 10 live proof at the worker level: the execute
    job sends the exact stored text via the mock provider.
    """
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    _staging_mock_config(monkeypatch, auto_reply=True)
    lead_id, phone = _make_lead(u1, name="Approve Lead")
    inbound_id = _make_inbound(u1, lead_id, phone, "Is it available?")
    stored = "Yes — still available. Tour Saturday at 11am?"
    approval_id = _insert_approval(u1, lead_id, inbound_id, draft_reply=stored)

    with _login_as(app_client, u1):
        resp = app_client.post(
            f"/api/harness/approvals/{approval_id}/approve"
        )
        assert resp.status_code == 200
        assert resp.get_json()["approval"]["id"] == approval_id

    # Run the execute-approval job the approve call enqueued.
    with _GateOpen():
        assert run_once() is True
    sent = sent_messages()
    assert len(sent) == 1
    assert sent[0]["body"] == stored
    assert sent[0]["to"] == phone


def test_inbox_reject_approval(app_client, two_users):
    u1, _ = two_users
    lead_id, phone = _make_lead(u1, name="Reject Lead")
    inbound_id = _make_inbound(u1, lead_id, phone, "Hello?")
    approval_id = _insert_approval(u1, lead_id, inbound_id)

    with _login_as(app_client, u1):
        resp = app_client.post(
            f"/api/harness/approvals/{approval_id}/reject",
            json={"reason": "not a fit"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["approval"]["status"] == "rejected"

    from harness.approvals import list_pending_approvals

    assert list_pending_approvals(u1) == []
    assert ("approval_rejected", "user") in _audit_actions(u1)


# --- Criterion 4: takeover ----------------------------------------------------


def test_takeover_set_and_clear_are_audited(app_client, two_users):
    u1, _ = two_users
    lead_id, phone = _make_lead(u1, name="Takeover Lead")
    _make_inbound(u1, lead_id, phone, "Hello?")

    with _login_as(app_client, u1):
        resp = app_client.post(
            f"/api/harness/inbox/conversations/{lead_id}/takeover",
            json={"active": True},
        )
        assert resp.status_code == 200
        assert resp.get_json()["conversation"]["human_takeover"] is True
        resp = app_client.post(
            f"/api/harness/inbox/conversations/{lead_id}/takeover",
            json={"active": False},
        )
        assert resp.status_code == 200
        assert resp.get_json()["conversation"]["human_takeover"] is False

    actions = _audit_actions(u1)
    assert ("harness_takeover_set", "user") in actions
    assert ("harness_takeover_cleared", "user") in actions
    assert inbox_mod.is_human_takeover(u1, lead_id) is False


def test_takeover_requires_bool_body(app_client, two_users):
    u1, _ = two_users
    lead_id, phone = _make_lead(u1, name="Takeover Validation Lead")
    _make_inbound(u1, lead_id, phone, "Hello?")
    with _login_as(app_client, u1):
        assert app_client.post(
            f"/api/harness/inbox/conversations/{lead_id}/takeover",
            json={"active": "yes"},
        ).status_code == 400
        assert app_client.post(
            f"/api/harness/inbox/conversations/{lead_id}/takeover",
            json={},
        ).status_code == 400


def test_takeover_forces_draft_in_authorized_mode_then_clearing_resumes(
    two_users, monkeypatch
):
    """Takeover: inbound drafts, no auto-send, even in Authorized mode.

    With takeover set, the worker job DRAFTs (approval created, no
    mock send). Clearing takeover resumes normal gating: the next
    inbound ALLOWs and sends via the mock provider.
    """
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    _staging_mock_config(monkeypatch, auto_reply=True)
    lead_id, phone = _make_lead(u1, name="Takeover Gate Lead")

    # Takeover ON: the auto-reply job must DRAFT, never send.
    assert inbox_mod.set_human_takeover(u1, lead_id, True) is not None
    inbound_id = _make_inbound(u1, lead_id, phone, "Is the house available?")
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={
            "analysis": _routine_analysis(),
            "insight_id": None,
            "error": None,
        },
    ):
        result = _run_auto_reply_job(u1, lead_id, inbound_id, "Is the house available?")
    assert result.get("decision") == "DRAFT"
    assert "taken over" in str(result.get("reason") or "").lower()
    assert result.get("sent") is False
    assert sent_messages() == []
    approvals = _approvals(u1)
    assert len(approvals) == 1
    assert approvals[0]["status"] == "pending"

    # Takeover OFF: the next inbound resumes normal gating and sends.
    inbox_mod.set_human_takeover(u1, lead_id, False)
    inbound_id2 = _make_inbound(u1, lead_id, phone, "Still available?")
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={
            "analysis": _routine_analysis(),
            "insight_id": None,
            "error": None,
        },
    ):
        result2 = _run_auto_reply_job(u1, lead_id, inbound_id2, "Still available?")
    assert result2.get("decision") == "ALLOW"
    assert result2.get("sent") is True
    sent = sent_messages()
    assert len(sent) == 1
    assert sent[0]["body"] == DRAFT_REPLY
    assert sent[0]["to"] == phone


def test_takeover_gate_decision_directly(two_users):
    """The policy gate itself DRAFTs under takeover (unit level)."""
    from harness.policy import decide

    u1, _ = two_users
    lead_id, phone = _make_lead(u1, name="Takeover Policy Lead")
    _make_inbound(u1, lead_id, phone, "Hello?")
    inbox_mod.set_human_takeover(u1, lead_id, True)

    decision, reason = decide(
        u1,
        "send_sms",
        "send_sms",
        {
            "mode": "authorized",
            "paused": False,
            "human_takeover": True,
            "consent_status": "verified",
            "opted_out": False,
            "suppressed": False,
            "quiet_hours": False,
            "rate_limited": False,
            "escalation_topics": [],
            "template_approved": False,
        },
    )
    assert decision == "DRAFT"
    assert "taken over" in reason.lower()
