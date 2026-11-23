"""Phase III-3 duplicate-outreach ledger tests.

Every follow-up test enqueues via ``schedule_follow_up_sms`` and runs
the job through the real harness worker (``run_once``), asserting on
durable ROWS (harness_outreach_ledger, harness_audit,
lead_activities, harness_follow_up_sends, harness_approvals) — never
on dashboard counters. The two red-before-green limbs (the
'drafted'-with-live-approval fire-time limb and the approval-execute
limb) are tested first: they fail on code without the ledger guard
and pass with it.
"""

import inspect
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import config
import crm_db
import db
import tenant_sms_db as tdb
from harness.outreach_ledger import (
    check_outreach_allowed,
    format_prior_outreach,
    record_outreach,
)
from lead_service import normalize_phone_e164, upsert_crm_lead
from sms_providers.mock import clear_sent_messages, sent_messages
from tests.test_follow_up_sms_sending import (
    BODY,
    REASON,
    REASON_OTHER,
    _approvals,
    _approval_row,
    _audit_rows,
    _enable_sending,
    _gate_open,
    _iso_in,
    _job_result,
    _make_follow_up,
    _make_lead,
    _mock_env,
    _run_due,
    _schedule,
    _send_row,
    _set_job_due,
)
from tests.test_harness_staging_safety import _set_settings
from workers.harness_worker import run_once

VOICE_REASON = "Follow up"


@pytest.fixture(autouse=True)
def _isolate_ledger():
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_follow_up_sends")
        conn.execute("DELETE FROM harness_outreach_ledger")
    clear_sent_messages()
    yield
    clear_sent_messages()


def _now():
    return datetime.now(timezone.utc)


def _ledger_rows(user_id, lead_id=None):
    with db.get_db() as conn:
        if lead_id is None:
            rows = conn.execute(
                "SELECT * FROM harness_outreach_ledger WHERE user_id = ? "
                "ORDER BY id",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM harness_outreach_ledger WHERE user_id = ? "
                "AND lead_id = ? ORDER BY id",
                (user_id, lead_id),
            ).fetchall()
    return [dict(r) for r in rows]


def _ledger_row_by_job(user_id, job_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_outreach_ledger WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        ).fetchone()
    return dict(row) if row else None


def _timeline_entries(user_id, lead_id, event_type=None):
    entries = crm_db.list_lead_activities(user_id, lead_id)
    if event_type:
        entries = [e for e in entries if e["event_type"] == event_type]
    return entries


def _timeline_payload(entry):
    raw = entry.get("payload_json") or "{}"
    return json.loads(raw) if isinstance(raw, str) else raw


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


# --- Migration 047 ------------------------------------------------------------


def test_migration_047_creates_ledger_table_and_phone_column(two_users):
    """Migration 047: the ledger table, its columns and indexes, plus
    leads.phone_normalized; the version is stamped."""
    with db.get_db() as conn:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(harness_outreach_ledger)").fetchall()}
        indexes = {r["name"] for r in conn.execute(
            "PRAGMA index_list(harness_outreach_ledger)").fetchall()}
        lead_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(leads)").fetchall()}
        stamped = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            ("047_outreach_ledger",),
        ).fetchone()
    assert {
        "id", "user_id", "lead_id", "channel", "reason", "reason_norm",
        "dedup_key", "phone_normalized", "status", "sms_message_id",
        "voice_call_id", "job_id", "approval_id", "scheduled_at",
        "sent_at", "created_at", "updated_at",
    } <= cols
    assert {
        "idx_outreach_ledger_user_job",
        "idx_outreach_ledger_lead_reason",
        "idx_outreach_ledger_lead",
        "idx_outreach_ledger_phone",
    } <= indexes
    assert "phone_normalized" in lead_cols
    assert stamped is not None


def test_migration_047_backfills_phone_and_ledger(two_users):
    """Backfill: leads.phone_normalized from phone_number; ledger rows
    from send rows (scheduled/claimed/drafted-live + sent-within-7-days;
    blocked/dead_letter/older-sent skipped). The conflict report finds
    two leads sharing one normalized phone."""
    import importlib

    m047 = importlib.import_module("migrations.versions.047_outreach_ledger")

    u1, _ = two_users
    now = _now()
    now_iso = now.isoformat()
    old_iso = (now - timedelta(days=8)).isoformat()
    fresh_iso = (now - timedelta(days=1)).isoformat()

    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_outreach_ledger")
        conn.execute("DELETE FROM harness_follow_up_sends")
        conn.execute("UPDATE leads SET phone_normalized = NULL")

    def _seed_lead(phone):
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO leads (user_id, name, phone_number, status, "
                "source, assigned_user_id, created_at, updated_at) "
                "VALUES (?, 'Seed', ?, 'new', 'test', ?, ?, ?)",
                (u1, phone, u1, now_iso, now_iso),
            )
            return cur.lastrowid

    lead_a = _seed_lead("(303) 870-3107")
    lead_b = _seed_lead("+13038703107")
    lead_c = _seed_lead("+13035550199")

    def _seed_send(lead_id, job_id, status, sent_at=None):
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO harness_follow_up_sends
                    (user_id, lead_id, job_id, reason, reason_norm,
                     status, scheduled_at, sent_at, created_at, updated_at)
                VALUES (?, ?, ?, 'Follow up', 'follow up', ?, ?, ?, ?, ?)
                """,
                (u1, lead_id, job_id, status, now_iso, sent_at, now_iso, now_iso),
            )

    _seed_send(lead_c, 9001, "scheduled")
    _seed_send(lead_c, 9002, "sent", fresh_iso)
    _seed_send(lead_c, 9003, "sent", old_iso)
    _seed_send(lead_c, 9004, "blocked")
    _seed_send(lead_c, 9005, "drafted")
    _seed_send(lead_c, 9006, "dead_letter")

    # Backfill via the migration's own helpers.
    with db.get_db() as conn:
        updated = m047._backfill_phone_normalized(conn, "sqlite")
        window_start = (now - timedelta(days=7)).isoformat()
        inserted = m047._backfill_ledger(conn, "sqlite", window_start)
    assert updated >= 3
    assert inserted == 3  # scheduled + sent-fresh + drafted

    with db.get_db() as conn:
        phones = {
            r["id"]: r["phone_normalized"]
            for r in conn.execute(
                "SELECT id, phone_normalized FROM leads WHERE user_id = ?",
                (u1,),
            ).fetchall()
        }
        ledger = _ledger_rows(u1)
    assert phones[lead_a] == "+13038703107"
    assert phones[lead_b] == "+13038703107"
    assert phones[lead_c] == "+13035550199"
    assert {r["job_id"] for r in ledger} == {9001, 9002, 9005}
    assert all(r["channel"] == "sms" for r in ledger)
    assert all(r["reason_norm"] == "follow up" for r in ledger)

    with db.get_db() as conn:
        m047._report_phone_conflicts(conn, "sqlite")
    # The report logs (does not return) — re-check via the helper.
    from harness.outreach_ledger import find_phone_conflicts

    with db.get_db() as conn:
        found = find_phone_conflicts(conn)
    assert any(
        f["user_id"] == u1 and f["phone_normalized"] == "+13038703107"
        for f in found
    )


# --- Normalization (criterion 3) ----------------------------------------------


def test_normalize_phone_e164_three_formats_and_garbage():
    """The three formats resolve to one E.164 number; garbage is
    un-normalizable ("")."""
    assert normalize_phone_e164("+1-303-870-3107") == "+13038703107"
    assert normalize_phone_e164("3038703107") == "+13038703107"
    assert normalize_phone_e164("(303) 870-3107") == "+13038703107"
    assert normalize_phone_e164("+13038703107") == "+13038703107"
    assert normalize_phone_e164("13038703107") == "+13038703107"
    assert normalize_phone_e164("") == ""
    assert normalize_phone_e164("123") == ""
    assert normalize_phone_e164("+123") == ""
    assert normalize_phone_e164("abc") == ""
    assert normalize_phone_e164("+1 (303) 870-3107 x123") == ""


def test_upsert_three_formats_returns_one_lead(two_users):
    """Upserting the same number in three formats returns the same
    lead id; the second and third are matches, not creates."""
    u1, _ = two_users
    first_id, created1, _ = upsert_crm_lead(
        u1, "+1-303-870-3107", {"name": "Format Lead"}, source="test",
        assigned_user_id=u1,
    )
    assert created1 is True
    second_id, created2, _ = upsert_crm_lead(
        u1, "3038703107", {"name": "Format Lead"}, source="test",
        assigned_user_id=u1,
    )
    third_id, created3, _ = upsert_crm_lead(
        u1, "(303) 870-3107", {"name": "Format Lead"}, source="test",
        assigned_user_id=u1,
    )
    assert second_id == first_id and created2 is False
    assert third_id == first_id and created3 is False
    assert len(db.list_leads(u1)) == 1
    assert db.get_lead(first_id, u1)["phone_normalized"] == "+13038703107"


def test_telnyx_format_variant_inbound_routes_to_existing_lead(
    two_users, monkeypatch
):
    """An inbound from a format variant routes to the existing lead —
    no duplicate lead is created (Telnyx path)."""
    import telnyx_webhooks as txwh
    from tests.test_telnyx_inbound_ai_workflow import (
        _inbound_payload,
        _lead,
        _setup_tenant,
    )

    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    # Unique number (not Ren's +13038703107, which dozens of other
    # tests hold as a lead across tenants in the shared suite DB —
    # that makes the platform-number routing ambiguous by design and
    # the deterministic most-recent-outbound choice lands elsewhere).
    lead_id, _ = _lead(u1, "+15551239902")
    before = len(db.list_leads(u1))
    result, status = txwh.handle_messaging_webhook(
        _inbound_payload("(555) 123-9902", account)
    )
    assert status == 200
    assert result["lead_id"] == lead_id
    assert len(db.list_leads(u1)) == before


def test_simpletexting_format_variant_inbound_routes_to_existing_lead(
    two_users, monkeypatch
):
    """Same routing guarantee on the SimpleTexting path."""
    import simpletexting_webhooks as stwh
    from tests.test_simpletexting_sms import _enable_st, _lead, _unique_e164

    u1, _ = two_users
    account = _enable_st(u1)
    lead_id, _ = _lead(u1, "+15551239901")
    before = len(db.list_leads(u1))
    payload = {
        "reportId": "r-fmt-1",
        "type": "INCOMING_MESSAGE",
        "values": {
            "messageId": f"st-msg-{uuid.uuid4().hex[:8]}",
            "text": "Interested",
            "accountPhone": "".join(c for c in account if c.isdigit()),
            "contactPhone": "(555) 123-9901",
        },
    }
    result, status = stwh.handle_inbound(payload)
    assert status == 200
    assert result["lead_id"] == lead_id
    assert len(db.list_leads(u1)) == before

# --- Ledger guard limbs (criterion 2) ------------------------------------------


def test_schedule_creates_ledger_row(two_users, monkeypatch):
    """Scheduling a follow-up creates the ledger row (sms, scheduled,
    job-linked, normalized phone)."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, phone = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    scheduled = _schedule(u1, lead_id, follow_up_id)
    rows = _ledger_rows(u1, lead_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "sms"
    assert row["status"] == "scheduled"
    assert row["reason_norm"] == "3-day check-in after showing"
    assert row["job_id"] == scheduled["job_id"]
    assert row["sms_message_id"] == scheduled["message_id"]
    assert row["phone_normalized"] == phone
    assert row["dedup_key"] == f"{u1}:{lead_id}:3-day check-in after showing"


def test_drafted_live_approval_blocks_second_follow_up(two_users, monkeypatch):
    """RED-BEFORE-GREEN (drafted limb): a second follow-up for the same
    triple BLOCKs while the first approval is pending — no second
    draft, no second approval. The audit names the prior send and the
    timeline carries the refusal."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="drafts_only", paused=False)
    lead_id, _ = _make_lead(u1)

    follow_up_a = _make_follow_up(u1, lead_id)
    sched_a = _run_due(u1, lead_id, follow_up_a)
    approval_a = _job_result(sched_a["job_id"])["approval_id"]
    assert _approval_row(approval_a)["status"] == "pending"
    ledger_a = _ledger_row_by_job(u1, sched_a["job_id"])
    assert ledger_a["status"] == "drafted"
    assert ledger_a["approval_id"] == approval_a

    follow_up_b = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=61)
    )
    sched_b = _schedule(
        u1, lead_id, follow_up_b, scheduled_at=_iso_in(minutes=61)
    )
    _set_job_due(sched_b["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    result_b = _job_result(sched_b["job_id"])
    assert result_b["decision"] == "BLOCK"
    assert result_b["reason"] == "duplicate_follow_up_send"
    assert result_b["approval_id"] is None
    assert len(_approvals(u1)) == 1
    assert _send_row(sched_b["job_id"])["status"] == "blocked"
    assert _ledger_row_by_job(u1, sched_b["job_id"])["status"] == "blocked"

    blocked = _audit_rows(u1, action="follow_up_sms_blocked")
    assert len(blocked) == 1
    assert f"prior sms send #{ledger_a['id']}" in blocked[0]["gate_reason"]
    assert "drafted" in blocked[0]["gate_reason"]
    refused = _timeline_entries(u1, lead_id, "duplicate_outreach_refused")
    assert len(refused) == 1
    payload = _timeline_payload(refused[0])
    assert payload["prior_ledger_id"] == ledger_a["id"]
    assert payload["prior_channel"] == "sms"
    assert payload["prior_status"] == "drafted"


def test_drafted_settled_approval_does_not_block(two_users, monkeypatch):
    """A 'drafted' row whose approval settled (rejected) stops
    blocking — the next follow-up drafts a new approval."""
    from harness.approvals import reject_approval

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="drafts_only", paused=False)
    lead_id, _ = _make_lead(u1)

    follow_up_a = _make_follow_up(u1, lead_id)
    sched_a = _run_due(u1, lead_id, follow_up_a)
    approval_a = _job_result(sched_a["job_id"])["approval_id"]
    _, error = reject_approval(u1, approval_a, reason="test reject")
    assert error is None

    follow_up_b = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=61)
    )
    sched_b = _schedule(
        u1, lead_id, follow_up_b, scheduled_at=_iso_in(minutes=61)
    )
    _set_job_due(sched_b["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    result_b = _job_result(sched_b["job_id"])
    assert result_b["decision"] == "DRAFT"
    assert result_b["approval_id"] is not None
    assert result_b["approval_id"] != approval_a
    assert len(_approvals(u1)) == 2


def test_approval_execute_refuses_second_pending_approval(two_users, monkeypatch):
    """RED-BEFORE-GREEN (execute-time limb): two pending approvals for
    the same triple — executing the first sends; executing the second
    refuses as a duplicate (no send, approval blocked, evidence names
    the first send)."""
    from harness.approvals import (
        approve_approval,
        enqueue_execute_approval_job,
    )
    from harness.followup_sends import _create_approval

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="drafts_only", paused=False)
    lead_id, _ = _make_lead(u1)

    # Two scheduled follow-ups for the same triple (different minutes
    # so schedule-time idempotency does not collapse them).
    follow_up_a = _make_follow_up(u1, lead_id)
    sched_a = _run_due(u1, lead_id, follow_up_a)
    approval_a = _job_result(sched_a["job_id"])["approval_id"]
    follow_up_b = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=61)
    )
    sched_b = _schedule(
        u1, lead_id, follow_up_b, scheduled_at=_iso_in(minutes=61)
    )
    # Seed the pre-fix state for B: a second pending approval for the
    # same triple, with its send + ledger rows 'drafted' behind it.
    send_b = _send_row(sched_b["job_id"])
    approval_b = _create_approval(
        u1,
        sched_b["job_id"],
        "follow_up_sms",
        {
            "draft_reply": BODY,
            "lead_id": lead_id,
            "follow_up_id": follow_up_b,
            "job_id": sched_b["job_id"],
            "follow_up_send_id": send_b["id"],
            "message_id": sched_b["message_id"],
            "gate_reason": "mode_drafts_only",
            "scheduled_at": sched_b["scheduled_at"],
        },
    )
    assert approval_b is not None
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_follow_up_sends SET status = 'drafted', "
            "approval_id = ? WHERE id = ?",
            (approval_b, send_b["id"]),
        )
        conn.execute(
            "UPDATE harness_outreach_ledger SET status = 'drafted', "
            "approval_id = ? WHERE job_id = ? AND user_id = ?",
            (approval_b, sched_b["job_id"], u1),
        )

    _, error_a = approve_approval(u1, approval_a)
    _, error_b = approve_approval(u1, approval_b)
    assert error_a is None and error_b is None
    job_a = enqueue_execute_approval_job(u1, approval_a)
    job_b = enqueue_execute_approval_job(u1, approval_b)
    assert job_a != job_b
    # Park B in the future so the first worker batch executes A only.
    _set_job_due(job_b, (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat())

    clear_sent_messages()
    _set_job_due(job_a, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    assert _approval_row(approval_a)["status"] == "executed"
    ledger_a = _ledger_row_by_job(u1, sched_a["job_id"])
    assert ledger_a["status"] == "sent"

    _set_job_due(job_b, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1  # no second send
    assert _approval_row(approval_b)["status"] == "blocked"
    assert _ledger_row_by_job(u1, sched_b["job_id"])["status"] == "blocked"
    blocked = _audit_rows(u1, action="approval_blocked")
    assert any(
        f"prior sms send #{ledger_a['id']}" in (r["gate_reason"] or "")
        for r in blocked
    )
    refused = _timeline_entries(u1, lead_id, "duplicate_outreach_refused")
    assert len(refused) == 1
    assert _timeline_payload(refused[0])["prior_ledger_id"] == ledger_a["id"]


def test_claimed_ledger_row_blocks(two_users, monkeypatch):
    """An in-flight 'claimed' ledger row for the triple blocks a
    second firing (no send, no draft, evidence names the prior row)."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    record_outreach(
        u1, lead_id=lead_id, channel="sms", reason=REASON,
        status="claimed", job_id=424242,
        scheduled_at=_now().isoformat(),
    )
    follow_up_id = _make_follow_up(u1, lead_id)
    sched = _run_due(u1, lead_id, follow_up_id)
    assert sent_messages() == []
    assert _job_result(sched["job_id"])["decision"] == "BLOCK"
    assert _job_result(sched["job_id"])["reason"] == "duplicate_follow_up_send"
    refused = _timeline_entries(u1, lead_id, "duplicate_outreach_refused")
    assert len(refused) == 1


def test_sent_within_window_blocks_with_evidence(two_users, monkeypatch):
    """A send within 7 days blocks the repeat; the audit + timeline
    name the prior sent row."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_a = _make_follow_up(u1, lead_id)
    sched_a = _run_due(u1, lead_id, follow_up_a)
    assert _job_result(sched_a["job_id"])["sent"] is True
    assert len(sent_messages()) == 1
    ledger_a = _ledger_row_by_job(u1, sched_a["job_id"])
    assert ledger_a["status"] == "sent"
    assert ledger_a["sent_at"] is not None

    follow_up_b = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=61)
    )
    sched_b = _schedule(
        u1, lead_id, follow_up_b, scheduled_at=_iso_in(minutes=61)
    )
    _set_job_due(sched_b["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 1
    assert _job_result(sched_b["job_id"])["decision"] == "BLOCK"
    blocked = _audit_rows(u1, action="follow_up_sms_blocked")
    assert len(blocked) == 1
    assert f"prior sms send #{ledger_a['id']}" in blocked[0]["gate_reason"]
    assert "sent" in blocked[0]["gate_reason"]
    refused = _timeline_entries(u1, lead_id, "duplicate_outreach_refused")
    assert len(refused) == 1
    payload = _timeline_payload(refused[0])
    assert payload["prior_ledger_id"] == ledger_a["id"]
    assert payload["prior_status"] == "sent"


def test_sent_older_than_window_sends(two_users, monkeypatch):
    """A sent row older than 7 days does not block the repeat."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_a = _make_follow_up(u1, lead_id)
    sched_a = _run_due(u1, lead_id, follow_up_a)
    assert len(sent_messages()) == 1
    aged = (_now() - timedelta(days=8)).isoformat()
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_follow_up_sends SET sent_at = ? WHERE job_id = ?",
            (aged, sched_a["job_id"]),
        )
        conn.execute(
            "UPDATE harness_outreach_ledger SET sent_at = ? WHERE job_id = ?",
            (aged, sched_a["job_id"]),
        )
    follow_up_b = _make_follow_up(
        u1, lead_id, due_at=_iso_in(minutes=61)
    )
    sched_b = _schedule(
        u1, lead_id, follow_up_b, scheduled_at=_iso_in(minutes=61)
    )
    _set_job_due(sched_b["job_id"], _iso_in(minutes=-1))
    with _gate_open():
        assert run_once() is True
    assert len(sent_messages()) == 2
    assert _job_result(sched_b["job_id"])["sent"] is True


def test_different_reason_lead_tenant_send_freely(two_users, monkeypatch):
    """Different reason, different lead, and different tenant (same
    phone + reason) all send freely inside the window."""
    _mock_env(monkeypatch)
    u1, u2 = two_users
    _enable_sending(u1)
    _enable_sending(u2)
    _set_settings(u1, mode="authorized", paused=False)
    _set_settings(u2, mode="authorized", paused=False)
    lead_id, phone = _make_lead(u1)
    follow_up_a = _make_follow_up(u1, lead_id)
    sched_a = _run_due(u1, lead_id, follow_up_a)
    assert len(sent_messages()) == 1

    # Different reason, same lead.
    follow_up_b = _make_follow_up(
        u1, lead_id, due_at=(datetime.now(timezone.utc) + timedelta(minutes=61)).isoformat(),
        reason=REASON_OTHER,
    )
    sched_b = _run_due(u1, lead_id, follow_up_b, reason=REASON_OTHER)
    assert len(sent_messages()) == 2
    assert _job_result(sched_b["job_id"])["sent"] is True

    # Different lead, same reason.
    lead2_id, _ = _make_lead(u1)
    follow_up_c = _make_follow_up(u1, lead2_id)
    sched_c = _run_due(u1, lead2_id, follow_up_c)
    assert len(sent_messages()) == 3
    assert _job_result(sched_c["job_id"])["sent"] is True

    # Different tenant, same phone + reason.
    lead_u2, _, _ = upsert_crm_lead(
        u2, phone, {"name": "Other Tenant Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=u2,
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = 'verified', "
            "opt_out_status = 'active' WHERE id = ? AND user_id = ?",
            (lead_u2, u2),
        )
    follow_up_d = _make_follow_up(u2, lead_u2)
    sched_d = _run_due(u2, lead_u2, follow_up_d)
    assert len(sent_messages()) == 4
    assert _job_result(sched_d["job_id"])["sent"] is True


# --- Channel-agnostic (criterion 1) + voice (Q5) ------------------------------


def test_voice_ledger_row_blocks_sms_follow_up(two_users, monkeypatch):
    """A sent voice row for the triple blocks an SMS follow-up for
    the same reason (channel-agnostic refusal)."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, phone = _make_lead(u1)
    record_outreach(
        u1, lead_id=lead_id, channel="voice", reason=VOICE_REASON,
        phone_normalized=phone, status="sent",
        voice_call_id=777, scheduled_at=_now().isoformat(),
        sent_at=_now().isoformat(),
    )
    follow_up_id = _make_follow_up(u1, lead_id, reason=VOICE_REASON)
    sched = _run_due(u1, lead_id, follow_up_id, reason=VOICE_REASON)
    assert sent_messages() == []
    assert _job_result(sched["job_id"])["decision"] == "BLOCK"
    blocked = _audit_rows(u1, action="follow_up_sms_blocked")
    assert f"prior voice send #" in blocked[0]["gate_reason"]


def test_sms_ledger_row_blocks_voice_call(app_client, two_users, monkeypatch):
    """A sent SMS row for the triple blocks a voice call for the same
    reason (409, provider never called, evidence written)."""
    from tests.test_voice_crm_leads import _persona, _profile

    u1, _ = two_users
    db.init_db()
    _profile(u1)
    persona_id = _persona(u1)
    lead_id, _, _ = upsert_crm_lead(
        u1, "+13038703107", {"name": "Voice Lead", "phone_number": "+13038703107"},
        source="test", touch_sms=True, assigned_user_id=u1,
    )
    record_outreach(
        u1, lead_id=lead_id, channel="sms", reason=VOICE_REASON,
        phone_normalized="+13038703107", status="sent",
        sms_message_id=1234, scheduled_at=_now().isoformat(),
        sent_at=_now().isoformat(),
    )
    calls = {"n": 0}

    def _never_start(*a, **k):
        calls["n"] += 1
        return {"provider_call_id": "must-not-happen"}

    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P", (), {"start_outbound_call": staticmethod(_never_start)}
        )(),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Voice Lead",
            "phone_number": "+13038703107",
            "call_purpose": VOICE_REASON,
            "compliance_confirmed": True,
        },
    )
    assert res.status_code == 409, res.get_json()
    assert calls["n"] == 0
    assert "prior sms send #" in res.get_json()["error"]
    blocked = _audit_rows(u1, action="voice_call_blocked")
    assert len(blocked) == 1
    refused = _timeline_entries(u1, lead_id, "duplicate_outreach_refused")
    assert len(refused) == 1


def test_voice_call_records_ledger_row(app_client, two_users, monkeypatch):
    """A started voice call records its ledger row (sent) so a later
    SMS follow-up for the same reason refuses."""
    from tests.test_voice_crm_leads import _persona, _profile

    u1, _ = two_users
    db.init_db()
    _profile(u1)
    persona_id = _persona(u1)
    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {
                "start_outbound_call": staticmethod(
                    lambda *a, **k: {"provider_call_id": "vapi_ledger_1"}
                )
            },
        )(),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Voice Lead",
            "phone_number": "+13038703107",
            "call_purpose": VOICE_REASON,
            "compliance_confirmed": True,
        },
    )
    assert res.status_code == 201, res.get_json()
    lead_id = res.get_json()["lead_id"]
    rows = _ledger_rows(u1, lead_id)
    assert len(rows) == 1
    assert rows[0]["channel"] == "voice"
    assert rows[0]["reason_norm"] == "follow up"
    assert rows[0]["status"] == "sent"
    assert rows[0]["voice_call_id"] is not None


# --- Record-only: auto-reply / sms_reply (Q2) -----------------------------------


def test_auto_reply_records_but_does_not_consult(two_users, monkeypatch):
    """The auto-reply path never consults the ledger (a conflicting
    'auto_reply' row does not stop the reply) but records its send."""
    import sms_ai_agent
    import sms_coach
    import telnyx_webhooks as txwh
    from sms_providers.telnyx import TelnyxSMSProvider
    from tests.test_duplicate_sms_send import txwh_inbound
    from tests.test_telnyx_inbound_ai_workflow import (
        _analysis,
        _lead,
        _setup_tenant,
    )

    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    lead_id, _ = _lead(u1, "+13038703107")
    record_outreach(
        u1, lead_id=lead_id, channel="sms", reason="auto_reply",
        status="sent", sent_at=_now().isoformat(),
        scheduled_at=_now().isoformat(),
    )
    assert len(_ledger_rows(u1, lead_id)) == 1

    result, _ = txwh_inbound("+13038703107", account)
    inbound_id = result["message_id"]
    with patch.object(
        sms_coach, "analyze_inbound_reply", return_value=_analysis()
    ), patch.object(TelnyxSMSProvider, "send_message") as send:
        send.return_value = {
            "provider_message_id": f"tx-out-{uuid.uuid4().hex[:8]}",
            "status": "queued",
        }
        outcome = sms_ai_agent.process_inbound_ai(
            u1, lead_id, inbound_id, "Tell me more", account
        )
    assert outcome["replied"] is True
    assert send.call_count == 1
    rows = [
        r for r in _ledger_rows(u1, lead_id) if r["reason_norm"] == "auto_reply"
    ]
    assert len(rows) == 2
    assert all(r["status"] == "sent" for r in rows)


# --- Ledger visibility (§8.1) ---------------------------------------------------


def test_ledger_endpoint_lists_rows(app_client, two_users, monkeypatch):
    """GET /api/crm/leads/<id>/outreach-ledger lists that lead's rows;
    another tenant's lead is 404."""
    _mock_env(monkeypatch)
    u1, u2 = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    _run_due(u1, lead_id, follow_up_id)

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.get(f"/api/crm/leads/{lead_id}/outreach-ledger")
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["lead_id"] == lead_id
    assert len(body["rows"]) == 1
    assert body["rows"][0]["status"] == "sent"
    assert body["rows"][0]["channel"] == "sms"

    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    res = app_client.get(f"/api/crm/leads/{lead_id}/outreach-ledger")
    assert res.status_code == 404


# --- Boundaries (criterion 1 matrix) ---------------------------------------------


def test_send_authorized_sms_contains_no_ledger_call():
    """Enforcement shape: the ledger check lives at the orchestration
    layer, never inside send_authorized_sms (it has no reason
    parameter and two send paths never call it)."""
    import sms_outbound

    source = inspect.getsource(sms_outbound.send_authorized_sms)
    assert "outreach_ledger" not in source
    assert "check_outreach_allowed" not in source


def test_process_inbound_ai_never_consults_but_records():
    """Auto-reply / sms_reply never consult the ledger (no
    check_outreach_allowed call) but record the send."""
    import sms_ai_agent

    source = inspect.getsource(sms_ai_agent.process_inbound_ai)
    assert "check_outreach_allowed" not in source
    assert "record_outreach" in source


def test_no_like_in_ledger_module():
    """III-2 lesson: the new SQL uses equality only — no LIKE in the
    ledger module outside its docstring."""
    import harness.outreach_ledger as mod

    source = inspect.getsource(mod)
    body = source.split('"""', 2)[2] if source.count('"""') >= 2 else source
    assert "like" not in body.lower()


def test_no_new_dependency():
    """No phonenumbers dependency was added (criterion 5)."""
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as fh:
        assert "phonenumbers" not in fh.read()


def test_format_prior_outreach_fits_audit_cap():
    """The prior-outreach naming fits the 500-char audit cap."""
    row = {
        "id": 42,
        "channel": "sms",
        "status": "sent",
        "sent_at": "2026-11-22T19:27:20Z",
        "job_id": 31,
        "reason_norm": "follow up",
    }
    text = format_prior_outreach(row)
    assert text == (
        "prior sms send #42 sent 2026-11-22T19:27:20Z "
        "(job #31, reason 'follow up')"
    )
    assert len(f"duplicate_follow_up_send: {text}") < 200


def test_check_outreach_allowed_unit_limbs(two_users):
    """Unit-level: the guard's limbs on seeded rows (no worker)."""
    u1, _ = two_users
    lead_id = 4242
    allowed, row, scope = check_outreach_allowed(u1, lead_id, "follow up")
    assert (allowed, row, scope) == (True, None, None)

    record_outreach(
        u1, lead_id=lead_id, channel="sms", reason="Follow up",
        status="sent", sent_at=(_now() - timedelta(days=8)).isoformat(),
        scheduled_at=_now().isoformat(),
    )
    allowed, _, _ = check_outreach_allowed(u1, lead_id, "follow up")
    assert allowed is True  # older than the window

    record_outreach(
        u1, lead_id=lead_id, channel="sms", reason="Follow up",
        status="sent", sent_at=_now().isoformat(),
        scheduled_at=_now().isoformat(),
    )
    allowed, row, scope = check_outreach_allowed(u1, lead_id, "follow up")
    assert allowed is False and scope == "within_window"

    # Execute-time: in-flight does not block the executor.
    allowed, _, _ = check_outreach_allowed(
        u1, 9999, "follow up", execute_time=True
    )
    assert allowed is True
    record_outreach(
        u1, lead_id=9999, channel="sms", reason="Follow up",
        status="scheduled", job_id=5150, scheduled_at=_now().isoformat(),
    )
    allowed, _, _ = check_outreach_allowed(
        u1, 9999, "follow up", execute_time=True
    )
    assert allowed is True
    allowed, _, scope = check_outreach_allowed(u1, 9999, "follow up")
    assert allowed is False and scope == "in_flight"
