"""TopAI launch load simulation — 10 / 100 / 1,000 tenants (mocked providers).

What it does
------------
For each tenant count N, it builds a fresh throwaway SQLite database, creates
N synthetic tenants, enqueues one realistic day of harness work per tenant,
and drains the queue with the REAL harness worker claim/execute loop
(``workers.harness_worker.process_one_batch`` -> ``harness.jobs.claim_jobs`` /
``complete_job`` / ``fail_job``) while 2 concurrent Flask test-client threads
hammer ``GET /api/harness/status`` (logged in) and ``GET /health``.
It measures enqueue cost, worker throughput, queue depth over time, web
latency (worker idle vs busy), dead-letter rate, cost-ledger behaviour and
SQLite lock contention.

What is REAL vs MOCKED (read before quoting any number)
--------------------------------------------------------
REAL: the durable queue (enqueue / claim / complete / fail / reclaim), the
cost ledger (``harness.costs.record_cost`` — one real row per completed job),
the Flask app + ``/api/harness/status`` handler, the worker batch loop.
MOCKED: the provider handlers. The real SMS handler would call Anthropic
(~0.8-2 s per reply) and Telnyx; the real follow-up/approval handlers hit
the DB and (for approvals) send real messages. Here each handler is replaced
by a sleep + a ledger write + ``{"ok": True}``. NO real API calls, SMS,
calls, emails or charges happen.
TIME-COMPRESSED: default mock latencies (sms 15 ms, follow-up 3 ms,
approval 8 ms) are ~100x faster than a real LLM call. Throughput measured
here is therefore an UPPER BOUND set by queue/DB overhead, not by provider
latency. Section "Real-latency projection" in the results file re-derives
throughput at realistic provider latencies — that projection, not the raw
simulated jobs/hour, is what sizes workers for production.
SQLite, single machine, single worker, gunicorn NOT involved (Flask
test_client is in-process; the 2 web threads approximate 2 gunicorn workers
only loosely). Production runs Postgres (FOR UPDATE SKIP LOCKED, row locks,
real connection pooling) — SQLite serialises writers with a single database
lock, so every lock error / latency inflation seen here is a SQLite
artifact until re-measured on Postgres. See loadsim-results.md.

Assumed daily job mix per tenant (one day, stated explicitly)
-------------------------------------------------------------
- sms_auto_reply (inbound SMS coaching):      8 / tenant / day
- scheduled_follow_up (lead follow-ups):     12 / tenant / day
- execute_approval (approved-action runs):    2 / tenant / day
Total: 22 jobs / tenant / day  ->  220 / 2,200 / 22,000 jobs per scale.
Mix rationale: a working agent gets a handful of inbound lead replies a day
and a dozen scheduled touches; approvals are rarer because most tenants run
Drafts Only. If real tenants average more inbound SMS, scale the sms share.

Deterministic failure injection: every 200th dispatched job returns
ok=False, retryable=False (permanent failure) to exercise the dead-letter
path. Expected dead-letter rate ~0.5%.

How to run
----------
    # from the repo root, with the project venv:
    .venv/bin/python scripts/load_sim.py [--scales 10 100 1000]
        [--max-seconds 600] [--out results/loadsim-run.json]
        [--sms-ms 15] [--followup-ms 3] [--approval-ms 8]
    # quick smoke (10 tenants only, ~1 min):
    .venv/bin/python scripts/load_sim.py --scales 10 --max-seconds 120

Output: JSON to --out (default results/loadsim-run.json) and a human
summary on stdout. A results write-up lives in docs/loadsim-results.md.
Exit code 0 even when a scale does not fully drain (that IS a result);
exit code 2 only on a harness/setup error.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

# Repo root (parent of scripts/) must be on sys.path: running this file puts
# scripts/ first, so `import db` would otherwise fail.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- Test env, set BEFORE importing app/config/db (mirrors tests/conftest.py) ---
_FD, _DB_PATH = tempfile.mkstemp(prefix="loadsim-", suffix=".db")
os.close(_FD)
os.environ["APP_ENV"] = "test"
os.environ["ENV"] = "test"
os.environ["DATABASE_PATH"] = _DB_PATH
os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key-not-real")
os.environ.setdefault("FLASK_SECRET_KEY", "test-flask-secret")
os.environ["SUBSCRIPTION_REQUIRED"] = "false"
os.environ["ALLOW_DESTRUCTIVE_DB_RESET"] = "false"
os.environ["ALLOW_SQLITE_TABLE_REBUILD"] = "false"
os.environ["RUN_DEMO_SEED_ON_STARTUP"] = "false"
os.environ["REGISTRATION_ENABLED"] = "true"
os.environ.pop("REGISTRATION_ALLOWLIST", None)

# Daily job mix per tenant (see module docstring).
MIX = {"sms_auto_reply": 8, "scheduled_follow_up": 12, "execute_approval": 2}
JOBS_PER_TENANT_PER_DAY = sum(MIX.values())
FAIL_EVERY_N = 200  # deterministic dead-letter injection


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(pct / 100 * len(ordered))))
    return ordered[idx]


class MockHandlers:
    """Mocked provider handlers: sleep + real cost-ledger write + ok.

    Installed onto harness.sms_handler / harness.followup / harness.approvals
    so workers.harness_worker.dispatch_job (which imports the handler from
    the module at dispatch time) calls the mock. No network, no SMS, no LLM.
    """

    def __init__(self, latencies_ms, counters):
        self.latencies = latencies_ms  # kind -> seconds
        self.counters = counters
        self._n = 0
        self._lock = threading.Lock()

    def _run(self, kind, job):
        import harness.costs as costs

        with self._lock:
            self._n += 1
            n = self._n
        time.sleep(self.latencies[kind])
        # Real ledger write, exactly as a real LLM/provider call would record.
        try:
            if kind == "sms_auto_reply":
                costs.record_anthropic_usage(
                    job.get("user_id"), "sms_auto_reply",
                    {"input_tokens": 1200, "output_tokens": 180},
                    model="claude-sonnet-4-5",
                )
            elif kind == "scheduled_follow_up":
                costs.record_cost(job.get("user_id"), "anthropic",
                                  "scheduled_follow_up", 900, "tokens",
                                  cost_cents=1)
            else:
                costs.record_cost(job.get("user_id"), "telnyx",
                                  "execute_approval", 1, "messages",
                                  cost_cents=1)
        except Exception as exc:  # ledger contention is a measured signal
            with self._lock:
                self.counters["ledger_errors"] += 1
                self.counters.setdefault("ledger_error_examples", []).append(str(exc)[:200])
        if n % FAIL_EVERY_N == 0:
            return {"ok": False, "reason": "simulated_permanent_failure",
                    "retryable": False}
        return {"ok": True, "sent": False, "decision": "draft",
                "reason": "mocked", "retryable": False}

    def sms(self, job):
        return self._run("sms_auto_reply", job)

    def followup(self, job):
        return self._run("scheduled_follow_up", job)

    def approval(self, job):
        return self._run("execute_approval", job)


def _pending_count():
    import db

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM harness_jobs WHERE status = 'pending'"
        ).fetchone()
        return int(row["c"])


def _status_counts():
    import db

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM harness_jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["c"]) for r in rows}


def _cost_rows():
    import db

    with db.get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM harness_costs").fetchone()
        return int(row["c"])


def run_scale(n_tenants, max_seconds, latencies_ms, web_threads=2):
    """Run one scale. Returns a results dict. Fresh tables per scale."""
    import auth
    import db
    import harness.jobs as jobs
    import harness.sms_handler as sms_handler
    import harness.followup as followup_mod
    import harness.approvals as approvals_mod
    from workers import harness_worker

    t0 = time.time()
    db.init_db()
    init_s = time.time() - t0

    # --- tenants ------------------------------------------------------------
    t0 = time.time()
    user_ids = []
    for i in range(n_tenants):
        uid = db.create_user(f"loadsim-{n_tenants}-{i}@example.com",
                             auth.hash_password("password123"))
        user_ids.append(uid)
    users_s = time.time() - t0

    # --- enqueue one day of jobs -------------------------------------------
    t0 = time.time()
    total_jobs = n_tenants * JOBS_PER_TENANT_PER_DAY
    for uid in user_ids:
        for kind, count in MIX.items():
            for k in range(count):
                jobs.enqueue_job(
                    uid, kind,
                    {"lead_id": 1, "inbound_id": 1,
                     "body": "simulated inbound",
                     "idempotency_key_note": f"sim-{kind}-{k}"},
                    idempotency_key=f"sim-{n_tenants}-{uid}-{kind}-{k}",
                )
    enqueue_s = time.time() - t0
    peak_depth = total_jobs

    # --- install mocks -------------------------------------------------------
    counters = {"ledger_errors": 0, "worker_errors": 0, "web_errors": 0,
                "db_locked": 0}
    mocks = MockHandlers(latencies_ms, counters)
    sms_handler.process_sms_auto_reply_job = mocks.sms
    followup_mod.process_scheduled_follow_up_job = mocks.followup
    approvals_mod.process_execute_approval_job = mocks.approval

    # --- web latency, worker idle -------------------------------------------
    from app import app, limiter

    app.config["TESTING"] = True
    limiter.enabled = False
    web_lat_idle = []
    web_lat_idle_by_path = {"/api/harness/status": [], "/health": []}

    def web_probe(client, lat_list, stop, results_key, by_path=None):
        try:
            r = client.post("/login", data={
                "email": f"loadsim-{n_tenants}-0@example.com",
                "password": "password123"}, follow_redirects=False)
            if r.status_code not in (200, 302):
                counters["web_errors"] += 1
        except Exception as exc:
            counters["web_errors"] += 1
            if "locked" in str(exc):
                counters["db_locked"] += 1
            return
        while not stop.is_set():
            for path in ("/api/harness/status", "/health"):
                t = time.time()
                try:
                    r = client.get(path)
                    ms = (time.time() - t) * 1000
                    lat_list.append(ms)
                    if by_path is not None:
                        by_path[path].append(ms)
                    if r.status_code >= 500:
                        counters["web_errors"] += 1
                except Exception as exc:
                    counters["web_errors"] += 1
                    if "locked" in str(exc):
                        counters["db_locked"] += 1
            time.sleep(0.05)

    stop_idle = threading.Event()
    idle_threads = []
    for _ in range(web_threads):
        th = threading.Thread(target=web_probe,
                              args=(app.test_client(), web_lat_idle, stop_idle, None,
                                    web_lat_idle_by_path),
                              daemon=True)
        th.start()
        idle_threads.append(th)
    time.sleep(5)
    stop_idle.set()
    for th in idle_threads:
        th.join(timeout=10)

    # --- drain with worker + web load ---------------------------------------
    depth_samples = [(0.0, total_jobs)]
    worker_done = threading.Event()
    worker_state = {"error": None}

    def worker_loop():
        try:
            while not worker_done.is_set():
                worked = harness_worker.process_one_batch(
                    "loadsim-worker", limit=10)
                if _pending_count() == 0:
                    break
                if not worked:
                    time.sleep(0.1)
        except Exception as exc:  # noqa: BLE001 - record, don't crash silently
            worker_state["error"] = str(exc)[:300]
            counters["worker_errors"] += 1
        finally:
            worker_done.set()

    web_lat_busy = []
    web_lat_busy_by_path = {"/api/harness/status": [], "/health": []}
    stop_busy = threading.Event()
    t_drain0 = time.time()
    wth = threading.Thread(target=worker_loop, daemon=True)
    wth.start()
    busy_threads = []
    for _ in range(web_threads):
        th = threading.Thread(target=web_probe,
                              args=(app.test_client(), web_lat_busy, stop_busy, None,
                                    web_lat_busy_by_path),
                              daemon=True)
        th.start()
        busy_threads.append(th)
    deadline = t_drain0 + max_seconds
    while wth.is_alive() and time.time() < deadline:
        time.sleep(1.0)
        try:
            depth_samples.append((time.time() - t_drain0, _pending_count()))
        except Exception:
            counters["db_locked"] += 1
    drained = not wth.is_alive()
    if not drained:
        worker_done.set()
    stop_busy.set()
    for th in busy_threads:
        th.join(timeout=10)
    wth.join(timeout=10)
    drain_s = time.time() - t_drain0
    depth_samples.append((drain_s, _pending_count()))

    counts = _status_counts()
    completed = counts.get("succeeded", 0)
    dead = counts.get("dead_letter", 0)
    pending = counts.get("pending", 0)
    claimed = counts.get("claimed", 0)
    cost_rows = _cost_rows()
    throughput_hr = (completed / drain_s * 3600) if drain_s > 0 else 0
    # Extrapolated full-drain time at the measured throughput.
    remaining = pending + claimed
    eta_full_s = (drain_s + remaining / (throughput_hr / 3600)
                  if throughput_hr > 0 else None)

    def lat_stats(vals):
        if not vals:
            return {"n": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        return {"n": len(vals), "p50_ms": round(_percentile(vals, 50), 1),
                "p95_ms": round(_percentile(vals, 95), 1),
                "max_ms": round(max(vals), 1)}

    return {
        "tenants": n_tenants,
        "jobs_per_tenant_per_day": JOBS_PER_TENANT_PER_DAY,
        "mix_per_tenant_per_day": dict(MIX),
        "jobs_enqueued": total_jobs,
        "init_db_seconds": round(init_s, 2),
        "create_users_seconds": round(users_s, 2),
        "enqueue_seconds": round(enqueue_s, 2),
        "enqueue_jobs_per_sec": round(total_jobs / enqueue_s, 1) if enqueue_s else None,
        "drain_seconds": round(drain_s, 2),
        "drained_fully": drained,
        "max_seconds_cap": max_seconds,
        "jobs_completed": completed,
        "jobs_dead_letter": dead,
        "jobs_pending_at_end": pending,
        "jobs_claimed_at_end": claimed,
        "dead_letter_rate": round(dead / total_jobs, 4) if total_jobs else 0,
        "throughput_jobs_per_hour": round(throughput_hr, 1),
        "extrapolated_full_drain_seconds": round(eta_full_s, 1) if eta_full_s else None,
        "extrapolated_full_drain_hours": round(eta_full_s / 3600, 2) if eta_full_s else None,
        "peak_queue_depth": max(d for _, d in depth_samples),
        "end_queue_depth": depth_samples[-1][1],
        "queue_depth_samples": [[round(t, 1), d] for t, d in depth_samples],
        "web_latency_idle_ms": lat_stats(web_lat_idle),
        "web_latency_busy_ms": lat_stats(web_lat_busy),
        "web_latency_idle_by_path_ms": {k: lat_stats(v) for k, v in web_lat_idle_by_path.items()},
        "web_latency_busy_by_path_ms": {k: lat_stats(v) for k, v in web_lat_busy_by_path.items()},
        "cost_ledger_rows": cost_rows,
        "ledger_errors": counters["ledger_errors"],
        "ledger_error_examples": counters.get("ledger_error_examples", [])[:3],
        "worker_errors": counters["worker_errors"],
        "worker_error": worker_state["error"],
        "web_errors": counters["web_errors"],
        "db_locked_events": counters["db_locked"],
        "mock_latency_ms": {k: v * 1000 for k, v in latencies_ms.items()},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scales", type=int, nargs="+", default=[10, 100, 1000])
    ap.add_argument("--max-seconds", type=float, default=600)
    ap.add_argument("--out", default="results/loadsim-run.json")
    ap.add_argument("--sms-ms", type=float, default=15)
    ap.add_argument("--followup-ms", type=float, default=3)
    ap.add_argument("--approval-ms", type=float, default=8)
    args = ap.parse_args(argv)

    latencies = {"sms_auto_reply": args.sms_ms / 1000,
                 "scheduled_follow_up": args.followup_ms / 1000,
                 "execute_approval": args.approval_ms / 1000}
    results = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "db": "SQLite (temp file, fresh per scale — see docstring)",
               "workers": 1, "web_threads": 2,
               "mock_latency_ms": {"sms_auto_reply": args.sms_ms,
                                   "scheduled_follow_up": args.followup_ms,
                                   "execute_approval": args.approval_ms},
               "scales": []}
    try:
        for n in args.scales:
            print(f"\n=== scale: {n} tenants "
                  f"({n * JOBS_PER_TENANT_PER_DAY} jobs) ===", flush=True)
            # Fresh DB per scale: drop + re-create tables by using a new file.
            # Simplest reliable route: delete rows from the harness tables and
            # users created by this script (init_db is idempotent, but the
            # previous scale's rows must go).
            import db as _db
            _db.init_db()
            with _db.get_db() as conn:
                for tbl in ("harness_costs", "harness_jobs", "harness_audit"):
                    try:
                        conn.execute(f"DELETE FROM {tbl}")
                    except Exception:
                        pass
                conn.execute("DELETE FROM users WHERE email LIKE 'loadsim-%'")
            res = run_scale(n, args.max_seconds, latencies)
            results["scales"].append(res)
            print(json.dumps({k: v for k, v in res.items()
                              if k != "queue_depth_samples"}, indent=2), flush=True)
    finally:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2))
        print(f"\nWrote {out}")
        try:
            os.remove(_DB_PATH)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
