"""Phase 1 harness durable job queue tests (harness/jobs.py) plus the
gated-SMS end-to-end test (enqueue -> worker run_once -> approval, no send).

All provider/Anthropic calls are mocked; nothing here sends a real SMS.
"""

import json
from unittest.mock import patch

import pytest

import db
from harness import jobs
from harness.jobs import (
    claim_jobs,
    complete_job,
    enqueue_job,
    fail_job,
    get_job,
    list_jobs,
    reclaim_stale_jobs,
)


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py), so
    earlier tests can leave harness jobs behind. Tests in this file
    assert on the queue as a whole (claim order, limits, emptiness), so
    clear the queue first: the assertions below are then about this
    test's jobs only."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    yield


def _make_due(job_id):
    """Force a failed job's next_attempt_at into the past so it is claimable."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET next_attempt_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job_id,),
        )


def _row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _unique_phone(prefix="303"):
    import uuid

    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


# --- Idempotent enqueue ------------------------------------------------------


def test_enqueue_is_idempotent_by_key(two_users):
    u1, _ = two_users
    first = enqueue_job(u1, "sms_auto_reply", {"a": 1}, idempotency_key="key-dup-1")
    second = enqueue_job(u1, "sms_auto_reply", {"a": 2}, idempotency_key="key-dup-1")
    assert first == second
    assert len(list_jobs(u1)) == 1
    assert json.loads(_row(first)["payload_json"]) == {"a": 1}
    complete_job(first)  # keep the shared queue clean for later claim tests


def test_enqueue_without_key_creates_distinct_jobs(two_users):
    u1, _ = two_users
    a = enqueue_job(u1, "sms_auto_reply", {"a": 1})
    b = enqueue_job(u1, "sms_auto_reply", {"a": 1})
    assert a != b
    assert len(list_jobs(u1)) == 2
    complete_job(a)
    complete_job(b)


def test_enqueue_same_key_for_different_users_returns_first_job(two_users):
    # M6: idempotency keys are scoped by (user_id, idempotency_key); a
    # second tenant reusing the same key gets its OWN new job, never the
    # first tenant's job id. Same-tenant reuse is still idempotent.
    u1, u2 = two_users
    first = enqueue_job(u1, "sms_auto_reply", {}, idempotency_key="key-shared-1")
    second = enqueue_job(u2, "sms_auto_reply", {}, idempotency_key="key-shared-1")
    assert first != second
    assert get_job(first, u2) is None
    assert get_job(second, u2) is not None
    assert get_job(second, u1) is None
    assert enqueue_job(u1, "sms_auto_reply", {}, idempotency_key="key-shared-1") == first
    complete_job(first, user_id=u1)
    complete_job(second, user_id=u2)


# --- Claim / complete round-trip --------------------------------------------


def test_claim_complete_round_trip(two_users):
    u1, _ = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {"lead_id": 7})
    claimed = claim_jobs(limit=10, claimed_by="test-worker")
    assert [c["id"] for c in claimed] == [job_id]
    assert claimed[0]["status"] == "claimed"
    assert claimed[0]["attempts"] == 1
    assert claimed[0]["claimed_by"] == "test-worker"
    # A claimed job is not claimable again until reclaimed/re-queued.
    assert claim_jobs(limit=10) == []
    complete_job(job_id, result={"ok": True})
    assert _row(job_id)["status"] == "succeeded"
    assert json.loads(_row(job_id)["result_json"]) == {"ok": True}


def test_claim_respects_limit(two_users):
    u1, _ = two_users
    ids = [enqueue_job(u1, "sms_auto_reply", {"n": i}) for i in range(3)]
    claimed = claim_jobs(limit=2, claimed_by="test-worker")
    assert len(claimed) == 2
    assert {c["id"] for c in claimed} <= set(ids)
    assert len(claim_jobs(limit=10)) == 1
    for job_id in ids:
        complete_job(job_id)


def test_complete_unknown_job_raises(two_users):
    with pytest.raises(ValueError):
        complete_job(999999999)


def test_fail_unknown_job_raises(two_users):
    with pytest.raises(ValueError):
        fail_job(999999999, "boom")


# --- Fail with backoff, then dead-letter -------------------------------------


def test_fail_retryable_backs_off_then_requeues(two_users):
    u1, _ = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {})
    claim_jobs(limit=1)
    status = fail_job(job_id, "transient boom", retryable=True)
    assert status == "pending"
    row = _row(job_id)
    assert row["status"] == "pending"
    assert row["next_attempt_at"]  # future backoff, not immediately due
    assert "transient boom" in (row["result_json"] or "")
    # Not claimable while its backoff window is in the future.
    assert claim_jobs(limit=1) == []
    _make_due(job_id)
    assert [c["id"] for c in claim_jobs(limit=1)] == [job_id]
    complete_job(job_id)


def test_fail_dead_letters_after_five_attempts(two_users):
    u1, _ = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {})
    for attempt in range(1, 6):
        claimed = claim_jobs(limit=1)
        assert len(claimed) == 1
        assert claimed[0]["attempts"] == attempt
        status = fail_job(job_id, f"boom {attempt}", retryable=True)
        if attempt < 5:
            assert status == "pending"
            _make_due(job_id)
        else:
            assert status == "dead_letter"
    row = _row(job_id)
    assert row["status"] == "dead_letter"
    assert row["attempts"] == 5
    # Dead-lettered jobs are never claimed again.
    assert claim_jobs(limit=10) == []


def test_non_retryable_fail_dead_letters_immediately(two_users):
    u1, _ = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {})
    # Claim this job the way the worker would. Other tests may have left
    # pending jobs behind in the shared session DB, so scope every
    # assertion to this job instead of assuming the queue is globally
    # empty: claim widely, then check only this job's state.
    claimed = claim_jobs(limit=100)
    assert job_id in {j["id"] for j in claimed}
    status = fail_job(job_id, "permanent: missing fields", retryable=False)
    assert status == "dead_letter"
    assert _row(job_id)["status"] == "dead_letter"
    # Dead-lettered jobs are never retried: no later claim returns it.
    assert job_id not in {j["id"] for j in claim_jobs(limit=100)}


# --- Stale-claim recovery -----------------------------------------------------


def test_reclaim_stale_jobs_requeues_old_claims(two_users):
    u1, _ = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {})
    claim_jobs(limit=1, claimed_by="crashed-worker")
    assert _row(job_id)["status"] == "claimed"
    # A just-claimed job is not stale.
    assert reclaim_stale_jobs(timeout_seconds=300) == 0
    assert _row(job_id)["status"] == "claimed"
    # Age the claim past the timeout: it is reclaimed and claimable again.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET claimed_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job_id,),
        )
    assert reclaim_stale_jobs(timeout_seconds=300) == 1
    row = _row(job_id)
    assert row["status"] == "pending"
    assert row["claimed_at"] is None
    assert row["claimed_by"] is None
    assert [c["id"] for c in claim_jobs(limit=1)] == [job_id]
    complete_job(job_id)


# --- Tenant scoping -----------------------------------------------------------


def test_get_job_and_list_jobs_are_user_scoped(two_users):
    u1, u2 = two_users
    job_a = enqueue_job(u1, "sms_auto_reply", {"owner": "a"})
    job_b = enqueue_job(u2, "sms_auto_reply", {"owner": "b"})
    assert get_job(job_a, u1)["id"] == job_a
    assert get_job(job_a, u2) is None
    assert get_job(job_b, u2)["id"] == job_b
    assert get_job(job_b, u1) is None
    assert get_job(999999999, u1) is None
    assert [j["id"] for j in list_jobs(u1)] == [job_a]
    assert [j["id"] for j in list_jobs(u2)] == [job_b]
    assert list_jobs(u1, status="pending") == list_jobs(u1)
    assert list_jobs(u1, status="succeeded") == []
    complete_job(job_a)
    complete_job(job_b)


# --- Worker: unknown kinds dead-letter ----------------------------------------


def test_worker_dead_letters_unknown_kind(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "definitely_not_a_kind", {"x": 1})
    assert run_once() is True
    assert _row(job_id)["status"] == "dead_letter"
    assert "unknown_kind" in (_row(job_id)["result_json"] or "")


# --- Gated SMS end-to-end (drafts_only) ---------------------------------------


def test_gated_sms_end_to_end_drafts_only(two_users):
    """Enqueue sms_auto_reply -> worker run_once with mocked coach + mocked
    provider send. Drafts Only must yield exactly one pending approval,
    one DRAFT audit row, a succeeded job, and zero send attempts."""
    from lead_service import upsert_crm_lead
    from workers.harness_worker import run_once

    u1, _ = two_users
    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        u1, phone, {"name": "E2E Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=u1,
    )
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
        idempotency_key=f"sms-auto-reply-{inbound_id}",
    )
    # Re-enqueueing the same inbound is a no-op (schedule_inbound_ai key).
    assert enqueue_job(
        u1, "sms_auto_reply", {"lead_id": lead_id, "inbound_id": inbound_id},
        idempotency_key=f"sms-auto-reply-{inbound_id}",
    ) == job_id

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
        assert not mock_send.called  # zero send attempts

    assert _row(job_id)["status"] == "succeeded"
    with db.get_db() as conn:
        approvals = conn.execute(
            "SELECT * FROM harness_approvals WHERE user_id = ? AND status = 'pending'",
            (u1,),
        ).fetchall()
        assert len(approvals) == 1
        draft = json.loads(dict(approvals[0])["draft_json"])
        assert draft["draft_reply"].startswith("Hi! Yes")
        assert draft["lead_id"] == lead_id
        assert draft["inbound_id"] == inbound_id
    from harness.audit import list_audit

    drafted = [
        r for r in list_audit(u1)
        if r["action"] == "sms_auto_reply_drafted" and r["gate_decision"] == "DRAFT"
    ]
    assert len(drafted) == 1
    assert drafted[0]["target_id"] == lead_id
    # No outbound SMS row was created for this lead by the handler.
    assert db.list_lead_messages(u1, lead_id) == []
