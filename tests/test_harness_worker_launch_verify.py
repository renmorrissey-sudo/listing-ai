"""Launch-readiness verification for the harness worker (blocker 1).

Added 2026-11-08 on branch ``launch-worker-verify``. Extends
``tests/test_harness_worker.py`` with the evidence the launch checklist
needs, all with mocked providers / synthetic data only:

* end-to-end processing through the REAL ``scheduled_follow_up`` handler
  (enqueue -> claim -> execute -> complete), not just a patched handler;
* the live-worker reclaim guard: a stale claim whose worker still has a
  fresh ``running`` heartbeat is NOT reclaimed (exactly-once for long
  jobs), and IS reclaimed once that heartbeat goes stale;
* the background heartbeat thread keeps liveness fresh while a long
  dispatch blocks the main loop;
* a real-process restart-recovery test for a job that SUCCEEDS after
  recovery (the existing subprocess test only covers the dead-letter
  path): attempts == 2 and exactly one audit row, never double-executed;
* a real-process web-latency test: a mocked 8 s AI job runs in a
  separate worker process while HTTP requests hit the real gunicorn web
  process; p50/p95 are measured with and without the job running.

Nothing here places a real call, sends a real SMS, or uses a real
provider key.
"""

import json
import os
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import db
from harness.jobs import claim_jobs, enqueue_job, reclaim_stale_jobs
from workers import harness_worker as hw


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_audit")
        conn.execute("DELETE FROM sms_worker_heartbeats WHERE worker_id LIKE 'harness-%'")
    yield


def _row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id):
    from lead_service import upsert_crm_lead

    phone = _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Launch Lead", "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    return lead_id


# --- (a) End-to-end processing through the real handler ---------------------


def test_worker_processes_job_end_to_end_with_real_handler(two_users):
    """Enqueue -> claim -> execute -> complete with the REAL
    scheduled_follow_up handler (no handler patch): the job succeeds, a
    CRM follow-up row is created for the lead, and one audit row is
    written. This is the worker's happy path with no mocks in the
    execution itself."""
    u1, _ = two_users
    lead_id = _make_lead(u1)
    job_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": lead_id})
    assert hw.run_once() is True
    row = _row(job_id)
    assert row["status"] == "succeeded"
    assert row["attempts"] == 1
    with db.get_db() as conn:
        follow_ups = conn.execute(
            "SELECT COUNT(*) AS n FROM lead_follow_ups WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()["n"]
        audits = conn.execute(
            "SELECT COUNT(*) AS n FROM harness_audit "
            "WHERE request_id = ? AND action = 'scheduled_follow_up_created'",
            (str(job_id),),
        ).fetchone()["n"]
    assert follow_ups == 1
    assert audits == 1
    assert hw.run_once() is False  # never re-executed


# --- (c) Live-worker reclaim guard + heartbeat thread -----------------------


def test_reclaim_skips_claim_of_live_worker_then_reclaims_when_stale(two_users):
    """Exactly-once for long jobs: a claim aged past the 300 s timeout is
    NOT reclaimed while its worker still has a fresh ``running``
    heartbeat (the worker is alive, just slow). Once that heartbeat goes
    stale, the same claim is reclaimed. A claim with no heartbeat row at
    all is reclaimed as before (crashed before first heartbeat)."""
    u1, _ = two_users
    job_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 123})
    claimed = claim_jobs(limit=1, claimed_by="harness-live-1")
    assert [c["id"] for c in claimed] == [job_id]
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET claimed_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job_id,),
        )

    # Live worker: fresh running heartbeat -> no reclaim.
    hw._touch_heartbeat("harness-live-1", "running")
    assert reclaim_stale_jobs(timeout_seconds=300) == 0
    assert _row(job_id)["status"] == "claimed"

    # Worker died: heartbeat stale -> reclaim.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE sms_worker_heartbeats "
            "SET last_seen_at = '2000-01-01T00:00:00+00:00' "
            "WHERE worker_id = ?",
            ("harness-live-1",),
        )
    assert reclaim_stale_jobs(timeout_seconds=300) == 1
    assert _row(job_id)["status"] == "pending"

    # No heartbeat row at all (crashed before first heartbeat): reclaim.
    # Claim all pending jobs (job 1 is pending again) and age only job 2.
    job_id2 = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 123})
    claimed = claim_jobs(limit=10, claimed_by="harness-never-heartbeated")
    assert job_id2 in [c["id"] for c in claimed]
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET claimed_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job_id2,),
        )
    assert reclaim_stale_jobs(timeout_seconds=300) == 1
    assert _row(job_id2)["status"] == "pending"
    assert _row(job_id)["status"] == "claimed"  # young claim untouched


def test_heartbeat_thread_stays_fresh_during_long_dispatch():
    """The background heartbeat thread keeps the liveness row fresh while
    the main loop is blocked in a long dispatch (simulated with sleep):
    the row's age stays within ~2x the thread interval the whole time,
    far inside the 120 s status freshness window."""
    worker_id = "harness-long-dispatch-test"
    stop, thread = hw._start_heartbeat_thread(worker_id, interval_seconds=0.5)
    try:
        time.sleep(3)  # main "loop" blocked, as a long AI job would block it
        with db.get_db() as conn:
            last_seen = conn.execute(
                "SELECT last_seen_at FROM sms_worker_heartbeats WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()["last_seen_at"]
        seen = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - seen).total_seconds()
        assert 0 <= age <= 2.0, f"heartbeat went stale during long dispatch: age={age}s"
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


# --- Real-process restart recovery: crashed job SUCCEEDS exactly once ------


def _proc_env(db_path):
    """Env for standalone worker/web processes against a temp SQLite DB
    (mirrors tests/conftest.py)."""
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


_SETUP_CRASHED_SUCCESS_JOB = r"""
import db
from harness.jobs import claim_jobs, enqueue_job
from lead_service import upsert_crm_lead

db.init_db()
user_id = db.create_user("launch-worker@example.com", "x" * 60)
lead_id, _, _ = upsert_crm_lead(
    user_id, "+13035550199",
    {"name": "Crash Recovery Lead", "phone_number": "+13035550199"},
    source="test", touch_sms=True, assigned_user_id=user_id,
)
job_id = enqueue_job(user_id, "scheduled_follow_up", {"lead_id": lead_id})
claimed = claim_jobs(limit=1, claimed_by="crashed-worker")
assert [c["id"] for c in claimed] == [job_id]
# Simulate the crash: claimed, never settled, claim aged past timeout.
# No heartbeat row for "crashed-worker": it died before heartbeating.
with db.get_db() as conn:
    conn.execute(
        "UPDATE harness_jobs SET claimed_at = '2000-01-01T00:00:00+00:00' "
        "WHERE id = ?",
        (job_id,),
    )
print(f"CRASHED_JOB={job_id} LEAD={lead_id}")
"""


def _sqlite_state(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            "SELECT * FROM harness_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audits = conn.execute(
            "SELECT COUNT(*) AS n FROM harness_audit "
            "WHERE action = 'scheduled_follow_up_created'"
        ).fetchone()["n"]
        follow_ups = conn.execute(
            "SELECT COUNT(*) AS n FROM lead_follow_ups"
        ).fetchone()["n"]
        beats = conn.execute(
            "SELECT worker_id, status, last_seen_at FROM sms_worker_heartbeats "
            "WHERE worker_id LIKE 'harness-%' ORDER BY last_seen_at DESC"
        ).fetchall()
        return (
            (dict(job) if job else None),
            audits,
            follow_ups,
            [dict(r) for r in beats],
        )
    finally:
        conn.close()


def test_real_worker_process_recovers_crashed_success_job_exactly_once(tmp_path):
    """Real-process restart recovery, success path: a real
    ``python -m workers.harness_worker`` subprocess reclaims a crashed
    ``scheduled_follow_up`` claim and executes the REAL handler. The job
    must succeed with attempts == 2 (crash claim + one recovery claim),
    exactly one follow-up and exactly one audit row must exist, and a
    second settle check must show no re-execution."""
    db_path = str(tmp_path / "worker-recovery.db")
    env = _proc_env(db_path)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    setup = subprocess.run(
        [sys.executable, "-c", _SETUP_CRASHED_SUCCESS_JOB],
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
        job, audits, follow_ups, beats = None, 0, 0, []
        while time.time() < deadline:
            job, audits, follow_ups, beats = _sqlite_state(db_path)
            if (
                job
                and job["status"] == "succeeded"
                and any(b["status"] == "running" for b in beats)
            ):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        assert proc.poll() is None, "worker exited before settling the job"
        assert job is not None and job["status"] == "succeeded"
        assert job["attempts"] == 2, f"expected exactly 2 attempts, got {job['attempts']}"
        assert follow_ups == 1, f"expected exactly 1 follow-up, got {follow_ups}"
        assert audits == 1, f"expected exactly 1 audit row, got {audits}"
        assert any(b["status"] == "running" for b in beats)
        # Second settle check: no re-execution after recovery.
        time.sleep(1)
        job2, audits2, follow_ups2, _ = _sqlite_state(db_path)
        assert job2["status"] == "succeeded" and job2["attempts"] == 2
        assert audits2 == 1 and follow_ups2 == 1
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
    _, _, _, beats = _sqlite_state(db_path)
    assert beats and beats[0]["status"] == "stopped"


# --- (d) Long AI job on the worker must not starve web requests ------------


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url, timeout=60):
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception as exc:  # noqa: BLE001 - readiness polling
            last_exc = exc
        time.sleep(0.5)
    raise AssertionError(f"server never became ready at {url}: {last_exc}")


def _measure_latencies(url, n):
    """Sequential GETs; returns a list of seconds per request."""
    latencies = []
    for _ in range(n):
        start = time.monotonic()
        with urllib.request.urlopen(url, timeout=30) as resp:
            resp.read()
            assert resp.status == 200
        latencies.append(time.monotonic() - start)
    return latencies


def _percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    idx = (len(ordered) - 1) * pct / 100.0
    lo, hi = int(idx), min(int(idx) + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


# Driver run in the worker process: enqueue one job, then run the worker
# with the AI handler replaced by a mocked 8 s "AI call". The driver is
# its own OS process, exactly like the production harness-worker service.
_WORKER_DRIVER = r"""
import sys, time
sys.path.insert(0, ".")
import db
from harness.jobs import enqueue_job

db.init_db()
job_id = enqueue_job(1, "scheduled_follow_up", {"lead_id": 123})
print(f"DRIVER_JOB={job_id}", flush=True)

from unittest.mock import patch
from workers import harness_worker as hw

def _slow_ai(job):
    print("HANDLER_START", flush=True)
    time.sleep(8)  # mocked long AI job (e.g. Claude draft generation)
    print("HANDLER_DONE", flush=True)
    return {"ok": True, "result": "done"}

with patch("harness.followup.process_scheduled_follow_up_job", side_effect=_slow_ai):
    worked = hw.run_once()
print(f"DRIVER_WORKED={worked}", flush=True)
"""


def test_long_ai_job_on_worker_does_not_starve_web(tmp_path):
    """Process isolation, measured: the production web command (gunicorn,
    2 workers) serves ``GET /health`` while a mocked 8 s AI job runs in
    a separate worker process. Web latency during the job must stay in
    the same band as the idle baseline (p95 well under 2 s) -- the job
    runs on the worker, never on the web process."""
    db_path = str(tmp_path / "latency.db")
    env = _proc_env(db_path)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    # The venv python driving this test is the main repo's .venv python;
    # gunicorn lives in the same venv.
    gunicorn_bin = os.path.join(os.path.dirname(sys.executable), "gunicorn")
    assert os.path.exists(gunicorn_bin), f"gunicorn not found at {gunicorn_bin}"

    # Pre-apply migrations single-process first: two gunicorn workers
    # racing db.init_db() on a FRESH SQLite file hit a UNIQUE constraint
    # on schema_migrations (pre-existing app boot race, SQLite-only;
    # production Postgres already has migrations applied before web
    # workers start). Pre-init makes the workers' init_db a no-op.
    setup = subprocess.run(
        [sys.executable, "-c", "import db; db.init_db()"],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert setup.returncode == 0, setup.stderr[-2000:]

    web = subprocess.Popen(
        [gunicorn_bin, "app:app", "--bind", f"127.0.0.1:{port}",
         "--workers", "2", "--timeout", "120"],
        cwd=repo_root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_http(f"{base}/health", timeout=60)
        baseline = _measure_latencies(f"{base}/health", 25)

        worker = subprocess.Popen(
            [sys.executable, "-c", _WORKER_DRIVER],
            cwd=repo_root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            # Wait until the worker has claimed the job and the mocked
            # AI call is actually running (driver prints HANDLER_START).
            deadline = time.time() + 30
            started = False
            # Poll the DB for the claim instead of parsing stdout, so
            # the pipe cannot block: the job must be claimed first.
            while time.time() < deadline:
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT status FROM harness_jobs ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[0] == "claimed":
                    started = True
                    break
                if worker.poll() is not None:
                    break
                time.sleep(0.2)
            assert started, "worker never claimed the long job"
            during = _measure_latencies(f"{base}/health", 25)
            worker.wait(timeout=30)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)
        out = worker.stdout.read() if worker.stdout else ""
        assert "HANDLER_START" in out and "HANDLER_DONE" in out, out[-2000:]
        assert "DRIVER_WORKED=True" in out, out[-2000:]

        conn = sqlite3.connect(db_path)
        try:
            job = conn.execute(
                "SELECT status, attempts FROM harness_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert job == ("succeeded", 1), f"long job did not complete: {job}"

        b50, b95 = _percentile(baseline, 50), _percentile(baseline, 95)
        d50, d95 = _percentile(during, 50), _percentile(during, 95)
        # Record the numbers in the test output for the launch report.
        print(
            f"\nWEB LATENCY baseline(n=25) p50={b50*1000:.1f}ms p95={b95*1000:.1f}ms "
            f"max={max(baseline)*1000:.1f}ms | "
            f"during 8s worker job(n=25) p50={d50*1000:.1f}ms p95={d95*1000:.1f}ms "
            f"max={max(during)*1000:.1f}ms"
        )
        assert d95 < 2.0, f"web p95 during long worker job too high: {d95:.2f}s"
        assert d50 < 1.0, f"web p50 during long worker job too high: {d50:.2f}s"
    finally:
        if web.poll() is None:
            web.terminate()
        try:
            web.wait(timeout=15)
        except subprocess.TimeoutExpired:
            web.kill()
            web.wait(timeout=10)
