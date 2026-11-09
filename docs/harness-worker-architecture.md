# AI Harness — Worker Architecture

**Date:** 2026-11-08
**Branch:** `topai-harness-hardening` (worktree `/tmp/hardening-worker`)
**Status:** Repo-side implementation complete and tested (see §7). The
Railway-dashboard side — whether a `harness-worker` service actually
exists in production — is **NOT VERIFIED** (§6): it needs a dashboard
read, which this work could not perform.

## 1. The three processes

The repo defines three processes. Railway services are created in the
Railway dashboard, not in the repo: `railway.toml` holds only the build
and per-service restart policy; the service-to-start-command mapping
lives in the dashboard (and, as the canonical record, in `Procfile`).

| Railway service | Procfile process | Start command | What it runs |
|---|---|---|---|
| `web` | `web` | `python -m migrations.runner && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` | Flask app (incl. `/harness` page + `GET /api/harness/status`) |
| `worker` | `worker` | `python -m workers.sms_campaign_worker` | SMS campaign worker: bulk campaign sends + quiet-hours-deferred one-to-one/AI SMS + 60-day listing-retention sweep |
| `harness-worker` | `harness-worker` | `python -m workers.harness_worker` | Harness worker: durable `harness_jobs` queue (SMS auto-reply drafts, scheduled follow-ups, approved-approval execution) |

Only `web` and `worker` existed before this work. The
`harness-worker` Procfile entry was added here; until a matching
Railway service is created with that start command, **no harness job
is ever processed in production** — nothing in `app.py` / `config.py`
starts the harness worker, and the SMS campaign worker contains zero
harness references.

## 2. Why the two workers cannot collide

* **Different queues, different tables.** The harness worker claims
  only from `harness_jobs` (`harness/jobs.py::claim_jobs`). The SMS
  campaign worker claims only from `sms_campaign_jobs`
  (`tenant_sms_db.claim_next_job`) plus due rows in `sms_messages`.
  Neither worker ever reads or writes the other's queue table.
* **Different claim paths.** Harness claims use Postgres
  `FOR UPDATE SKIP LOCKED` (`_claim_one_postgres`) or a guarded
  `SELECT` + `UPDATE ... WHERE status = 'pending'` on SQLite
  (`_claim_one_sqlite`); campaign claims use their own guarded claim
  in `tenant_sms_db`. Two harness-worker replicas racing for one job
  still cannot double-claim: the first claim wins, the second gets
  nothing (tested: `test_two_workers_cannot_claim_the_same_job`).
* **Different heartbeat ids.** Both workers write liveness rows to
  `sms_worker_heartbeats` (migration 016), but the harness worker's
  id is `harness-<hostname>-<pid>` while the SMS worker's is
  `<hostname>-<pid>`. The status check matches `worker_id LIKE
  'harness-%'` only, so a running SMS worker can never make the
  harness worker look alive (tested).
* **Different failure domains.** A crash in one worker's handler
  cannot settle the other's jobs: harness job mutations are
  ownership-checked (`complete_job` / `fail_job` with `user_id`;
  cross-tenant raises `PermissionError` and leaves the job untouched).

## 3. Harness worker loop (workers/harness_worker.py)

One iteration (`process_one_batch`, also exposed as `run_once` for
tests/smoke):

1. **Reclaim stale claims** — `harness.jobs.reclaim_stale_jobs()`
   moves jobs left `claimed`/`running` past 300 s back to `pending`
   (attempts untouched, so the retry budget still applies). A killed
   worker never loses work silently.
2. **Claim** up to 10 due pending jobs (`CLAIM_LIMIT = 10`),
   `claimed_by = harness-<hostname>-<pid>`. Claiming increments
   `attempts` — the retry budget is counted at claim time.
3. **Dispatch by kind**: `sms_auto_reply` →
   `harness.sms_handler.process_sms_auto_reply_job`;
   `scheduled_follow_up` →
   `harness.followup.process_scheduled_follow_up_job`;
   `execute_approval` →
   `harness.approvals.process_execute_approval_job`. Unknown kinds
   dead-letter immediately (never silently dropped).
4. **Settle by handler contract**: `ok` → `complete_job`;
   `ok=False, retryable=True` → `fail_job(retryable=True)` with
   exponential backoff `min(2**attempts, 120)` minutes;
   `ok=False, retryable=False` → `fail_job(retryable=False)` →
   `dead_letter`. After `MAX_ATTEMPTS = 5` attempts a retryable
   failure also dead-letters. Dead-letter transitions fire the M4 ops
   alert (webhook if configured, audit + log otherwise).

The loop heartbeats every iteration (see §4), logs a heartbeat line
every 30 loops, handles `SIGINT`/`SIGTERM` by finishing the current
iteration and exiting cleanly, and never lets a handler crash,
provider failure, or alert failure break the loop — every failure
path settles the job (retryable) or dead-letters it.

### Scope / policy enforcement at execution time

* Every job is dispatched with its own `user_id`; handlers and the
  policy gate (`harness.policy.decide`) are per-tenant. The worker
  claims across tenants by design, but no job can escape its tenant:
  job reads (`get_job`, `list_jobs`) and mutations
  (`complete_job`, `fail_job`) are `user_id`-scoped, and the worker
  passes the claimed job's `user_id` into its mutations.
* `execute_approval` jobs re-check the policy gate at execution time
  with a fresh lead read (`harness/approvals.py::execute_approved`):
  a lead that opted out, a tenant that paused, or a draft that became
  invalid after approval blocks the send (`blocked`, never sent).
  The approval claim itself (`approved` → `executing`) is a guarded
  UPDATE, so a second execution finds no approved row and sends
  nothing — at-most-once send per approval.
* In Drafts Only, `sms_auto_reply` jobs only ever produce a draft +
  pending approval; the send path is never reached.

## 4. Heartbeat and liveness

The harness worker writes a durable heartbeat row on every loop
iteration (plus `running` at boot and `stopped` on clean shutdown)
via `tenant_sms_db.touch_worker_heartbeat` into
`sms_worker_heartbeats` (migration 016, already applied in
production). The write is wrapped so a heartbeat failure can never
break job processing. Since 2026-11-08 (`launch-worker-verify`, §10)
a daemon thread in `main()` additionally heartbeats every 10 s,
because the loop alone cannot heartbeat while it is blocked inside a
long dispatch — without the thread, a job running longer than the
120 s freshness window would make a live worker look dead.

`GET /api/harness/status` (and the `/harness` page, rendered from the
same dict) surfaces it as:

```json
"worker": {"running": true, "last_seen_at": "2026-11-08T...", "worker_id": "harness-...-1234"}
```

`running` is True only when the freshest `harness-%` row has
`status='running'` and `last_seen_at` within the last 120 s (the
worker heartbeats every 1–10 s idle). No row / stale row / `stopped`
row all report `running: false`.

## 5. Environment variables (harness-worker service)

Same database and secrets as `web`/`worker`; nothing harness-specific
beyond the alert webhook. Set on the `harness-worker` service:

| Variable | Value / notes |
|---|---|
| `DATABASE_URL` | Production Postgres URL (same as `web`). Never SQLite in production (`config.py` refuses). |
| `APP_ENV` | `production` |
| `FLASK_SECRET_KEY` | Same as `web` (required at boot by `config.validate_config()`). |
| `ANTHROPIC_API_KEY` | Required: draft generation calls Claude; usage is metered into `harness_costs`. |
| `SMS_PROVIDER` | `telnyx` (production default). |
| `TELNYX_API_KEY`, `TELNYX_MESSAGING_PROFILE_ID` | As on `web`; the worker sends only through the gated paths. |
| `SMS_AI_AUTO_REPLY_ENABLED` | Leave at the platform default; the harness mode/pause is the primary control. |
| `HARNESS_ALERT_WEBHOOK_URL` | Slack (or other HTTPS POST) endpoint for M4 dead-letter / provider-failure / worker-failure alerts. Unset = audit + log only. |
| `HARNESS_ALERTS_ENABLED` | Leave unset (enabled by default). |
| `VAPI_API_KEY`, `SENDGRID_API_KEY` | Only if the deployment uses voice/email execution paths; the worker itself does not need them for SMS-only tenants. |

Do **not** set `ALLOW_DESTRUCTIVE_DB_RESET`, `ALLOW_SQLITE_TABLE_REBUILD`,
or `RUN_DEMO_SEED_ON_STARTUP` to `true`. Do **not** copy staging values
into production or vice versa.

## 6. Railway setup + verification (dashboard steps — Ren's access needed)

The repo cannot create Railway services. In the Railway dashboard,
project `gracious-expression`:

1. **Create the service:** New Service → GitHub Repo → same
   repository/branch as production (`main`) → name it
   **`harness-worker`** → set Start Command to
   `python -m workers.harness_worker` (the Procfile `harness-worker`
   process). Set the variables in §5 (copy from the `web` service;
   never paste a `DATABASE_URL` into chat or tickets).
2. **Deploy** (it deploys on creation; otherwise Deployments →
   Deploy). Confirm the deployment succeeds and the service shows
   running.
3. **Confirm the worker is running** — any one of these, in order of
   strength:
   - **Heartbeat (strongest):** query production Postgres:
     ```sql
     SELECT worker_id, status, last_seen_at
       FROM sms_worker_heartbeats WHERE worker_id LIKE 'harness-%';
     ```
     Expect one row, `status='running'`, `last_seen_at` within the
     last 2 minutes. (Or open `/harness` / `GET /api/harness/status`
     and read the `worker.running` field.)
   - **Logs:** the `harness-worker` service Deploy Logs show
     `Harness worker starting id=harness-...` and a `Harness worker
     heartbeat` line every 30 loops; no `harness.jobs unavailable`
     warning (that warning means the harness tables/module are
     missing).
   - **Jobs drain:** enqueue a test `sms_auto_reply` in Drafts Only
     (or check the existing queue): `pending` jobs decrease and a
     draft + pending approval appear; `harness_jobs` rows move to
     `succeeded` with no `dead_letter` rows.
4. **Confirm the other services are untouched:** `worker` still runs
   `python -m workers.sms_campaign_worker` (its logs show `SMS
   campaign worker starting`), `web` still runs gunicorn. The two
   workers' heartbeat rows are distinguishable by the `harness-`
   prefix (§2).

### Open question — production harness-worker existence (R7)

Whether a production service running `python -m workers.harness_worker`
already exists could **not** be verified from the repo: the repo
records only `web` + `worker` in `Procfile`/`railway.toml`, and no
Railway dashboard access was available to this work. The checklist in
`docs/harness-railway-checklist.md` (R7) already records this as the
single most important dashboard check. If step 1 above finds the
service already exists, record its actual start command and variables
in that checklist's §7 log and skip creation; if it does not exist,
production harness jobs have never been processed and the service
must be created before any autonomous mode is offered.

## 7. Restart / recovery behavior

| Event | Behavior |
|---|---|
| Service crash / OOM / deploy | Railway `restartPolicyType = ON_FAILURE`, up to 10 retries (`railway.toml` `[deploy]`), per service. On restart the worker re-runs `db.init_db()` (idempotent migrations) and resumes polling. |
| SIGTERM (deploy / scale / stop) | Graceful: finishes the current iteration, writes a `stopped` heartbeat row, logs `Harness worker stopped cleanly`, exits 0. |
| Worker killed mid-job (SIGKILL, crash, deploy during dispatch) | The job stays `claimed` with its `claimed_at`. The next worker pass (same or new process) reclaims it after the 300 s stale timeout and executes it — exactly once per claim, and the approval claim guard prevents a double send. A job is never silently lost. Since §10, reclaim additionally skips claims whose worker still has a fresh `running` heartbeat, so a live worker running a job longer than 300 s cannot have it reclaimed mid-execution. |
| Handler throws | Caught per job: provider-failure alert counted, job failed retryable with backoff. The loop continues with the next job. |
| Handler returns retryable failure 5 times | Dead-letter; M4 alert fires once; the job is never retried again. |
| Unknown job kind | Dead-letter immediately with `unknown_kind` in `result_json`. |
| DB unreachable at boot | `db.init_db()` failure is logged and the worker keeps running (idle + retry); the loop's own exceptions are caught, alerted, and retried after 5 s. Jobs are not lost — they stay `pending` in Postgres. |
| Two harness-worker replicas | Safe: `FOR UPDATE SKIP LOCKED` (Postgres) / guarded claim (SQLite) means only one replica claims each job. Running two replicas is allowed but unnecessary; one service is the intended topology. |

## 8. Tests

`tests/test_harness_worker.py` — 10 tests, all passing 2026-11-08 on
this branch (test SQLite; synthetic data only; no real sends):

* Worker-driven retries to dead-letter after 5 attempts; non-retryable
  failure dead-letters at once.
* Restart recovery: crashed claim reclaimed and executed exactly once
  (attempts 2: crash claim + recovery claim), second pass does not
  re-execute.
* Two workers cannot claim the same job (claim-level and dispatch-level).
* Worker-scoped mutations reject a wrong-tenant `user_id` (job left
  untouched); unknown-job mutation is a no-op, not a crash.
* Approval execution blocked when the tenant pauses after approving
  (policy re-check; audit `approval_blocked` with a pause reason).
* Durable heartbeat written and reported via `get_harness_status`;
  SMS-worker heartbeats never make the harness worker look alive.
* Real-process test: a `python -m workers.harness_worker` subprocess
  boots, heartbeats, reclaims a crashed unknown-kind claim to
  `dead_letter` (attempts exactly 2), and exits 0 with a `stopped`
  heartbeat after SIGTERM.

### 8a. Launch verification, 2026-11-08 (branch `launch-worker-verify`)

Two real gaps were found in the §3/§7 design and fixed on this branch
(see §10); the new tests live in
`tests/test_harness_worker_launch_verify.py` (5 tests):

* **End-to-end, real handler:** enqueue → claim → execute → complete
  through the real `scheduled_follow_up` handler (no handler patch):
  job `succeeded` with attempts 1, exactly 1 CRM follow-up row and
  exactly 1 `scheduled_follow_up_created` audit row; a second pass
  does not re-execute.
* **Retries / dead-letter** (existing tests, re-run): retryable handler
  failure retried with backoff, dead-letter after 5 attempts
  (`test_worker_retries_to_dead_letter_after_five_attempts`, 5 handler
  calls); non-retryable failure dead-letters on the first pass.
* **Restart recovery, success path (real process):** a real
  `python -m workers.harness_worker` subprocess reclaims a crashed
  `scheduled_follow_up` claim and executes the real handler: job
  `succeeded`, attempts exactly 2, exactly 1 follow-up row, exactly 1
  audit row; a second settle check shows no re-execution; SIGTERM
  exits 0 with a `stopped` heartbeat.
* **Live-worker reclaim guard:** a claim aged past 300 s is NOT
  reclaimed while its worker has a fresh `running` heartbeat; it IS
  reclaimed once that heartbeat goes stale; a claim with no heartbeat
  row is reclaimed as before.
* **Heartbeat thread:** with the main loop blocked 3 s (simulating a
  long dispatch), the heartbeat row's age stays ≤ 2 s, far inside the
  120 s status window.
* **Web latency under a long worker job (measured, real processes):**
  gunicorn web (production command, 2 workers) serving `GET /health`
  while a mocked 8 s AI job runs in a separate worker process —
  25 sequential requests each way: baseline p50 1.3 ms / p95 2.9 ms /
  max 8.2 ms; during the job p50 1.7 ms / p95 6.4 ms / max 26.2 ms.
  The long job runs on the worker, never on the web process; web
  requests are not starved. (SQLite test DB; the isolation property
  proven is separate OS processes — the same topology as production.)

Targeted harness/worker run (8 files: `test_harness_worker`,
`test_harness_worker_launch_verify`, `test_harness_jobs`,
`test_harness_job_scoping`, `test_harness_tenant_isolation`,
`test_harness_approvals`, `test_harness_alerts`,
`test_harness_alerts_e2e`): **86 passed, seed 1234** and **86 passed,
seed 777**. Full suite: **1161 passed, 6 skipped, seed 1234**
(9:50; base 1156 + the 5 new tests in this file).

Full suite on this branch: see the hardening report (seed 1234 run).

## 9. Related docs

* `docs/harness-railway-checklist.md` — R7 (production worker
  existence), R8 (alert webhook), and the §7 completion log where
  dashboard observations must be recorded.
* `docs/harness-staging.md` — staging services; its `staging-worker`
  uses the same start command as the production `harness-worker`.
* `workers/harness_worker.py`, `harness/jobs.py`,
  `harness/approvals.py::execute_approved`,
  `harness/routes.py::get_harness_status` — implementation.

## 10. Fixes made during launch verification (2026-11-08, `launch-worker-verify`)

The service definition itself (§1, §5, §6) was verified correct and
complete as-is: the `harness-worker` Procfile entry, the Railway
restart policy (`ON_FAILURE`, 10 retries), the env-var table, and the
status-API wiring (`worker` key, 120 s freshness, `harness-%` match)
all check out, and no config change was needed. Two gaps were found
in the worker's long-job behavior and fixed in code:

1. **Stale heartbeat during a long dispatch (liveness).** The loop
   heartbeats only *between* batches, so a single dispatch lasting
   over 120 s (a slow AI call) would make a live, working worker
   report `worker.running=false`. Fix:
   `workers/harness_worker.py::_start_heartbeat_thread` — a daemon
   thread heartbeating every 10 s from `main()`, stopped and joined
   on clean shutdown before the `stopped` row is written.
2. **Reclaim could double-execute a long job (exactly-once).**
   `reclaim_stale_jobs()` reclaimed any claim older than 300 s,
   including one held by a live worker still executing it — a second
   claim would then run the same job twice. Fix:
   `harness/jobs.py::reclaim_stale_jobs` now skips claims whose
   `claimed_by` matches a `sms_worker_heartbeats` row with
   `status='running'` and `last_seen_at` within 120 s
   (`HEARTBEAT_FRESH_SECONDS`, same window as the status API). A dead
   worker's heartbeat goes stale within 120 s, so its jobs are still
   reclaimed; claims with no heartbeat row are reclaimed as before;
   if the heartbeat table is missing the guard is skipped and the
   plain reclaim runs (logged warning) rather than stranding jobs.

Both fixes are covered by the §8a tests. No Procfile, `railway.toml`,
or env-var changes were needed.
