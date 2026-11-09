"""Harness worker architecture tests (workers/harness_worker.py).

Covers the worker-level behaviour the jobs-module tests do not: retries
driven end-to-end through the worker to dead-letter, restart recovery
(a crashed claim is reclaimed and executed exactly once), two workers
never double-claiming the same job, worker-side scoped mutations
rejecting a wrong-tenant user_id, the approval execution-time policy
re-check when the tenant pauses after approving, the durable worker
heartbeat (and its surfacing in get_harness_status), and a real
``python -m workers.harness_worker`` subprocess that boots, heartbeats,
reclaims a crashed claim, and stops cleanly on SIGTERM.

All provider/Anthropic calls are mocked or avoided (unknown-kind and
scheduled_follow_up jobs never touch a provider); nothing here sends a
real SMS. Synthetic data only, in the shared test SQLite database
(except the subprocess test, which uses its own temp database file).
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import db
from harness import jobs as jobs_module
from harness.jobs import (
    claim_jobs,
    complete_job,
    enqueue_job,
    fail_job,
)
from workers import harness_worker as hw


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py). Clear
    the harness queue first so each test's worker runs see only its own
    jobs."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    yield


def _row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _make_due(job_id):
    """Force a retry-queued job's next_attempt_at into the past."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET next_attempt_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job_id,),
        )


def _age_claim(job_id):
    """Age a claimed job's claimed_at past the 300 s stale timeout,
    simulating a worker that died mid-job without settling it."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET claimed_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job_id,),
        )


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


# --- Worker-driven retries: bounded attempts, then dead-letter -------------


def test_worker_retries_to_dead_letter_after_five_attempts(two_users):
    """A handler that always fails retryably is retried by the worker
    with backoff and dead-letters after MAX_ATTEMPTS (5) claims -- the
    retry budget is enforced by the worker loop, not just by fail_job
    called directly (covered in test_harness_jobs)."""
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 123})
    calls = {"n": 0}

    def _always_transient(job):
        calls["n"] += 1
        return {"ok": False, "retryable": True, "reason": "transient boom"}

    with patch(
        "harness.followup.process_scheduled_follow_up_job",
        side_effect=_always_transient,
    ):
        for attempt in range(1, 6):
            assert run_once() is True
            row = _row(job_id)
            assert row["attempts"] == attempt
            if attempt < 5:
                assert row["status"] == "pending"
                assert row["next_attempt_at"]  # backoff, not immediately due
                _make_due(job_id)
            else:
                assert row["status"] == "dead_letter"
        # Dead-lettered jobs are never picked up again.
        assert run_once() is False
    assert calls["n"] == 5


def test_worker_non_retryable_failure_dead_letters_at_once(two_users):
    """A handler that fails non-retryably dead-letters on the first
    worker pass -- no retry budget is spent on permanent failures."""
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 123})
    with patch(
        "harness.followup.process_scheduled_follow_up_job",
        return_value={"ok": False, "retryable": False, "reason": "lead_missing"},
    ):
        assert run_once() is True
    row = _row(job_id)
    assert row["status"] == "dead_letter"
    assert row["attempts"] == 1
    assert run_once() is False


# --- Restart recovery: crashed claim reclaimed, executed exactly once ------


def test_worker_restart_recovery_executes_crashed_job_exactly_once(two_users):
    """Kill/restart simulation: worker A claims a job and dies without
    settling it. The next worker pass reclaims the stale claim and
    executes the job exactly once -- never lost, never double-executed."""
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 123})
    # Worker A claims, then "crashes": no complete/fail, no heartbeat.
    claimed = claim_jobs(limit=1, claimed_by="crashed-worker")
    assert [c["id"] for c in claimed] == [job_id]
    assert _row(job_id)["status"] == "claimed"
    # A fresh claim cannot take it while the claim is young.
    assert claim_jobs(limit=1, claimed_by="worker-b") == []
    _age_claim(job_id)

    calls = {"n": 0}

    def _succeed(job):
        calls["n"] += 1
        return {"ok": True, "result": "done"}

    with patch(
        "harness.followup.process_scheduled_follow_up_job",
        side_effect=_succeed,
    ):
        # One pass: reclaim (1 stale) + claim + dispatch + complete.
        assert run_once() is True
    assert calls["n"] == 1
    row = _row(job_id)
    assert row["status"] == "succeeded"
    assert row["attempts"] == 2  # crash claim + recovery claim; reclaim itself adds 0
    # A second pass must not execute it again.
    with patch(
        "harness.followup.process_scheduled_follow_up_job",
        side_effect=_succeed,
    ):
        assert run_once() is False
    assert calls["n"] == 1


def test_two_workers_cannot_claim_the_same_job(two_users):
    """Two workers racing for one pending job: the first claim wins and
    the second gets nothing (SQLite guarded UPDATE / Postgres
    FOR UPDATE SKIP LOCKED). The SMS campaign worker claims from a
    different table entirely, so it can never take a harness job."""
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 123})
    first = claim_jobs(limit=10, claimed_by="worker-a")
    assert [c["id"] for c in first] == [job_id]
    assert claim_jobs(limit=10, claimed_by="worker-b") == []
    complete_job(job_id)
    # And at dispatch level: two worker passes, one execution.
    job_id2 = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 123})
    calls = {"n": 0}

    def _succeed(job):
        calls["n"] += 1
        return {"ok": True}

    with patch(
        "harness.followup.process_scheduled_follow_up_job",
        side_effect=_succeed,
    ):
        assert run_once() is True
        assert run_once() is False
    assert calls["n"] == 1
    assert _row(job_id2)["status"] == "succeeded"


# --- Worker-side scoped mutations -------------------------------------------


def test_worker_scoped_mutation_rejects_wrong_tenant(two_users):
    """The worker passes the claimed job's user_id into complete/fail,
    so a job dict whose user_id does not match the stored owner leaves
    the job untouched (jobs-module PermissionError, caught inside the
    worker helpers) instead of mutating another tenant's job."""
    u1, u2 = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {"x": 1})
    claim_jobs(limit=1, claimed_by="harness-worker-test")
    before = _row(job_id)
    assert before["status"] == "claimed"

    # Wrong tenant: both helpers must leave the job exactly as it was.
    hw._fail_job(jobs_module, {"id": job_id, "user_id": u2}, "boom", retryable=True)
    hw._complete_job(jobs_module, {"id": job_id, "user_id": u2}, {"ok": True})
    after = _row(job_id)
    assert after["status"] == "claimed"
    assert after["attempts"] == before["attempts"]
    assert after["result_json"] == before["result_json"]

    # The owning tenant's mutation still works through the same helpers.
    hw._complete_job(jobs_module, {"id": job_id, "user_id": u1}, {"ok": True})
    assert _row(job_id)["status"] == "succeeded"


def test_worker_unknown_job_mutation_is_a_noop_not_a_crash(two_users):
    """A worker helper call for a job id that no longer exists must not
    raise out of the worker loop (jobs-module ValueError, caught)."""
    u1, _ = two_users
    hw._fail_job(jobs_module, {"id": 999999999, "user_id": u1}, "boom", retryable=True)
    hw._complete_job(jobs_module, {"id": 999999999, "user_id": u1}, {"ok": True})


# --- Approval execution-time policy re-check: pause after approval ---------


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _insert_approval(user_id, lead_id, inbound_id):
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
                        "draft_reply": "Hi! Yes, it is still available.",
                        "lead_id": lead_id,
                        "inbound_id": inbound_id,
                    }
                ),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def _approval(approval_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return dict(row) if row else None


@contextmanager
def _gate_open():
    """Force the execution-time gate inputs open (no quiet hours, no rate
    limit, not suppressed) so the test isolates the pause itself."""
    with patch("sms_quiet_hours.in_quiet_hours", return_value=False), patch(
        "sms_authorization._rate_limited", return_value=False
    ), patch("tenant_sms_db.is_suppressed", return_value=False):
        yield


def test_execution_blocked_when_tenant_pauses_after_approval(app_client, two_users):
    """Execution-time policy re-check, pause variant: the approval was
    granted while the account was live, but the tenant pauses before
    the worker executes it. The worker must block the send (never
    execute on a stale approval) and audit the pause as the reason.
    Opt-out-after-approval is covered in test_harness_approvals; this
    covers the pause/mode side of the same re-check."""
    from workers.harness_worker import run_once

    u1, _ = two_users
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        u1, phone, {"name": "Pause Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=u1,
    )
    approval_id = _insert_approval(u1, lead_id, 424250)
    _login(app_client, u1)
    resp = app_client.post(f"/api/harness/approvals/{approval_id}/approve")
    assert resp.status_code == 200
    assert _approval(approval_id)["status"] == "approved"

    # Pause between approval and execution.
    from harness.policy import get_settings

    get_settings(u1)  # ensure the settings row exists
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_settings SET paused = 1 WHERE user_id = ?", (u1,)
        )

    with _gate_open(), patch(
        "sms_ai_agent.process_inbound_ai",
        return_value={"replied": True, "reason": "sent"},
    ) as mock_send:
        assert run_once() is True
        assert not mock_send.called
    assert _approval(approval_id)["status"] == "blocked"
    from harness.audit import list_audit

    blocked = [r for r in list_audit(u1) if r["action"] == "approval_blocked"]
    assert len(blocked) == 1
    assert "paused" in (blocked[0]["gate_reason"] or "")


# --- Durable heartbeat + status surfacing -----------------------------------


def _clear_harness_heartbeats():
    with db.get_db() as conn:
        conn.execute("DELETE FROM sms_worker_heartbeats WHERE worker_id LIKE 'harness-%'")


def test_harness_worker_heartbeat_written_and_reported(two_users):
    """The worker's durable heartbeat row is written with the
    harness-<hostname>-<pid> id, and get_harness_status reports it as
    running while fresh, not-running when stale, and absent when no
    worker has ever heartbeated (e.g. the service was never created)."""
    from harness.routes import get_harness_status

    u1, _ = two_users
    _clear_harness_heartbeats()
    try:
        status = get_harness_status(u1)["worker"]
        assert status == {"running": False, "last_seen_at": None, "worker_id": None}

        hw._touch_heartbeat("harness-test-1", "running")
        with db.get_db() as conn:
            row = dict(
                conn.execute(
                    "SELECT * FROM sms_worker_heartbeats WHERE worker_id = ?",
                    ("harness-test-1",),
                ).fetchone()
            )
        assert row["status"] == "running"
        assert json.loads(row["metadata_json"])["service"] == "harness_worker"

        status = get_harness_status(u1)["worker"]
        assert status["running"] is True
        assert status["worker_id"] == "harness-test-1"
        assert status["last_seen_at"] == row["last_seen_at"]

        # Stale heartbeat (last seen long ago): not running.
        with db.get_db() as conn:
            conn.execute(
                "UPDATE sms_worker_heartbeats "
                "SET last_seen_at = '2000-01-01T00:00:00+00:00' "
                "WHERE worker_id = ?",
                ("harness-test-1",),
            )
        assert get_harness_status(u1)["worker"]["running"] is False

        # Clean stop: status stopped, also not running.
        hw._touch_heartbeat("harness-test-1", "stopped")
        assert get_harness_status(u1)["worker"]["running"] is False
    finally:
        _clear_harness_heartbeats()


def test_harness_heartbeat_never_collides_with_sms_worker(two_users):
    """The SMS campaign worker's heartbeat id has no harness- prefix,
    so its rows are invisible to the harness status check -- a running
    SMS worker must never make the harness worker look alive."""
    import tenant_sms_db as tdb

    from harness.routes import get_harness_status

    u1, _ = two_users
    _clear_harness_heartbeats()
    try:
        tdb.touch_worker_heartbeat("somehost-123", status="running")
        assert get_harness_status(u1)["worker"]["running"] is False
        hw._touch_heartbeat("harness-somehost-456", "running")
        assert get_harness_status(u1)["worker"]["running"] is True
    finally:
        _clear_harness_heartbeats()
        with db.get_db() as conn:
            conn.execute(
                "DELETE FROM sms_worker_heartbeats WHERE worker_id = ?",
                ("somehost-123",),
            )


# --- Real worker process: boot, heartbeat, reclaim, SIGTERM -----------------


def _worker_env(db_path):
    """Env for a standalone `python -m workers.harness_worker` process
    against its own temp SQLite database (mirrors tests/conftest.py)."""
    env = dict(os.environ)
    env["APP_ENV"] = "test"
    env["ENV"] = "test"
    env["DATABASE_PATH"] = db_path
    env.pop("DATABASE_URL", None)
    env.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key-not-real")
    env.setdefault("FLASK_SECRET_KEY", "test-flask-secret")
    env["SUBSCRIPTION_REQUIRED"] = "false"
    env["ALLOW_DESTRUCTIVE_DB_RESET"] = "false"
    env["ALLOW_SQLITE_TABLE_REBUILD"] = "false"
    env["RUN_DEMO_SEED_ON_STARTUP"] = "false"
    env["REGISTRATION_ENABLED"] = "true"
    return env


_SETUP_CRASHED_JOB = r"""
import db
from harness.jobs import claim_jobs, enqueue_job

db.init_db()
job_id = enqueue_job(1, "definitely_not_a_kind", {"x": 1})
claimed = claim_jobs(limit=1, claimed_by="crashed-worker")
assert [c["id"] for c in claimed] == [job_id]
# Simulate the crash: claimed, never settled, claim aged past timeout.
with db.get_db() as conn:
    conn.execute(
        "UPDATE harness_jobs SET claimed_at = '2000-01-01T00:00:00+00:00' "
        "WHERE id = ?",
        (job_id,),
    )
print(f"CRASHED_JOB={job_id}")
"""


def _sqlite_state(db_path):
    """Read job + heartbeat state straight from the worker's DB file."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            "SELECT * FROM harness_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        beats = conn.execute(
            "SELECT worker_id, status, last_seen_at FROM sms_worker_heartbeats "
            "WHERE worker_id LIKE 'harness-%' ORDER BY last_seen_at DESC"
        ).fetchall()
        return (dict(job) if job else None), [dict(r) for r in beats]
    finally:
        conn.close()


def test_real_worker_process_reclaims_crashed_job_and_stops_cleanly(tmp_path):
    """End-to-end at the process level (no in-process run_once): start
    a real `python -m workers.harness_worker` subprocess against a temp
    DB holding a crashed claim. The worker must reclaim it, dispatch
    it (unknown kind -> dead-letter, never silently dropped), write a
    durable heartbeat while alive, and exit 0 with a `stopped`
    heartbeat after SIGTERM."""
    db_path = str(tmp_path / "worker-e2e.db")
    env = _worker_env(db_path)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    setup = subprocess.run(
        [sys.executable, "-c", _SETUP_CRASHED_JOB],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert setup.returncode == 0, setup.stderr[-2000:]
    assert "CRASHED_JOB=" in setup.stdout

    proc = subprocess.Popen(
        [sys.executable, "-m", "workers.harness_worker"],
        cwd=repo_root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 60
        job, beats = None, []
        while time.time() < deadline:
            job, beats = _sqlite_state(db_path)
            if (
                job
                and job["status"] == "dead_letter"
                and any(b["status"] == "running" for b in beats)
            ):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        assert proc.poll() is None, "worker exited before settling the job"
        assert job is not None and job["status"] == "dead_letter"
        assert "unknown_kind" in (job["result_json"] or "")
        # The crashed claim was reclaimed and re-claimed exactly once
        # more: attempts 2, not a runaway retry loop.
        assert job["attempts"] == 2
        assert any(b["status"] == "running" for b in beats)
        # A second settle check: the dead-lettered job is never re-run.
        time.sleep(1)
        job2, _ = _sqlite_state(db_path)
        assert job2["status"] == "dead_letter" and job2["attempts"] == 2
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0, f"worker exit={proc.returncode} out={out[-2000:]}"
    assert "Harness worker starting" in out
    assert "stopped cleanly" in out
    _, beats = _sqlite_state(db_path)
    assert beats and beats[0]["status"] == "stopped"
