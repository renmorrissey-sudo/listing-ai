# AI Harness — Railway Checklist (H5/H6 Readiness)

**Date:** 2026-11-08
**Branch:** `topai-harness-critical-fixes` (HEAD `3f51da2`)
**Status:** Code/docs verification complete. **Every Railway-dashboard item below is
NOT VERIFIED** — no Railway access or credentials were available to this work.
Nothing here was observed in the live dashboard; navigation paths are the
standard Railway dashboard layout as described in the repo docs and Railway's
public docs, and are marked where they could not be confirmed against the
live project.

**Scope:** H5 (backup/restore never exercised, no pre-deploy backup evidence)
and H6 (rollback never tested in staging; no staging environment exists).
This checklist is the complete list of what Ren (or someone with Railway
access) must do or provide. Do not mark H5/H6 closed until every row in
§7 is checked off with a date, an observed result, and (for backups) an
artifact record.

**Project:** `gracious-expression` (Railway project name, per
`docs/harness-staging.md` §1 and `docs/DATABASE_SAFETY.md`). Production
services in the repo: `web` (Procfile `web`) and `worker` (Procfile `worker`).
Service display names in the dashboard may differ from these — confirm
against the dashboard and correct this file if they do.

**Safety rules for every step below:**
- Never point staging at the production Postgres database.
- Never run `DROP TABLE`, `DROP DATABASE`, `TRUNCATE`, or a destructive
  down-migration in staging or production. There is no down-migration for
  harness migrations 029/030/031 and none will be written.
- Never set `ALLOW_DESTRUCTIVE_DB_RESET`, `ALLOW_SQLITE_TABLE_REBUILD`,
  or `RUN_DEMO_SEED_ON_STARTUP` to `true` in staging or production.
- Never paste a full `DATABASE_URL` (it contains the password) into chat,
  a ticket, a log, or this repo.
- All `pg_dump`/`pg_restore` work happens from a local machine or a
  scratch environment, never from inside the production app container,
  and dump files are stored outside the container.

---

## 1. Precise list of Railway access / configuration required

| # | Item | What it is | Where it lives in Railway | Permission / role needed | Value / setting required |
|---|---|---|---|---|---|
| R1 | Staging web service | New service `staging`, same GitHub repo as production, deploying the harness branch (see step A) | Project `gracious-expression` → New Service → GitHub Repo | Workspace/project access that can create services, edit variables, and deploy (typically Owner or Admin; confirm in Railway → workspace Settings → Members — role names NOT VERIFIED against the live workspace) | Start command: `python -m migrations.runner && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` (same as production `web`). Env vars per §2 of `docs/harness-staging.md` (see step A). |
| R2 | Staging worker service | New service `staging-worker`, same repo/branch as R1 | Project `gracious-expression` → New Service → GitHub Repo | Same as R1 | Start command: `python -m workers.harness_worker`. Same env vars as R1 (both services need `DATABASE_URL`, `APP_ENV=staging`, secrets). |
| R3 | Staging Postgres | New Railway Postgres database, separate from production | Project `gracious-expression` → New → Database → PostgreSQL | Same as R1 | Its own `DATABASE_URL`, injected into R1/R2 only. Never the production `DATABASE_URL`. |
| R4 | Production Postgres backup/snapshot settings + retention | Whether automated backups/snapshots are enabled on the production Postgres, the retention window, and whether point-in-time recovery is available | Production Postgres service → Backups tab (or Settings, if the plan shows it there) | Read access to the Postgres service; plan change (if backups need enabling) needs Owner/Admin | Record the observed setting. If no Backups option exists, automated backups are not available on the current plan — `pg_dump` (R5) is then the only backup, and that must be recorded. |
| R5 | Pre-deploy `pg_dump` | A manual logical backup of the production Postgres taken before the next production deploy | Taken from a local machine using the production `DATABASE_URL` / public URL copied from the Postgres service Variables (see step C) | Ability to read the production `DATABASE_URL` from the dashboard (or Railway CLI access to the project) | `pg_dump "$DATABASE_URL" --format=custom --file="backup-<YYYYMMDD-HHMMSS>.dump"` (exact command in `docs/DATABASE_SAFETY.md`). Record date, size, SHA-256, and storage location. |
| R6 | Scratch Postgres for restore exercise | A throwaway Postgres to restore the R5 dump into — either a second Railway Postgres in the project, a local Postgres, or the staging Postgres **only if it holds no data worth keeping** (restoring wipes the target) | Railway project (new Postgres service) or a local machine | Same as R1 for a Railway scratch DB; none beyond a local Postgres install for a local restore | `pg_restore --clean --if-exists --no-owner --dbname="<scratch DATABASE_URL>" backup-<...>.dump`, then row-count spot checks (see step D). |
| R7 | Production harness-worker service — existence check | **Open question:** the repo's `Procfile` worker runs `python -m workers.sms_campaign_worker`, which contains zero harness references, and nothing in `app.py`/`config.py` starts the harness worker. Harness jobs are processed only if a separate Railway service with start command `python -m workers.harness_worker` was manually created. | Project `gracious-expression` → Services list | Read access to the project services list | Confirm whether such a service exists and what its start command and variables are. If it does not exist, production harness jobs are never processed — record that, and do not offer any autonomous mode until a worker service is created and verified. (The assessment plan, line 388, records that production worker deployment was a Phase 1 gate decision, not an automatic deploy.) |
| R8 | Alert webhook URL (`HARNESS_ALERT_WEBHOOK_URL`) | The destination for M4 operational failure alerts (dead-letter jobs, provider failures after 3 consecutive failures, worker failures). Code side is complete and tested (`harness/alerts.py`, `tests/test_harness_alerts.py` — 14 passed). With the variable unset, alerts still go to the audit log + structured logs, but nothing external is notified. | Production service(s) that run harness code → Variables. At minimum the service running the harness worker (R7); set it on `web` too if the web process ever sends alerts. Same variable on staging services for the staging demo (pointing at a test channel, never the production channel). | Ability to create a webhook URL in the receiving app (e.g. Slack) and to edit Railway service variables | A Slack incoming-webhook URL (or any HTTPS endpoint that accepts a POST of a JSON object). Set as `HARNESS_ALERT_WEBHOOK_URL=<url>`. Leave `HARNESS_ALERTS_ENABLED` unset (enabled is the default); set it to `false` only to deliberately silence external sends. Verify with a forced dead-letter in staging first (step F). |
| R9 | Pre-Phase-1 deployment still redeployable | The production deployment of commit `7f760b8` (2026-11-07, "Fix /generate-script crash…") — the last deployment before Phase 1 (`1755347`, 2026-11-08) — still listed in Railway and redeployable | Production `web` service → Deployments | Read access to Deployments; redeploy action needs deploy permission | Confirm the `7f760b8` deployment (or the deployment immediately preceding `1755347`) is listed and shows a Redeploy action, and note the retention limit (how far back deployments are kept). If it has expired, `git revert` + push is the only rollback path — record that. |
| R10 | Railway CLI (optional, not required) | Alternative to dashboard clicks for `pg_dump` and deploys | Local machine, after `railway login` + `railway link` to `gracious-expression` | Railway account with project access | Optional. The dashboard path in each step below is the primary route; CLI is a convenience, not a prerequisite. CLI availability on Ren's machine is NOT VERIFIED. |

**What is NOT needed:** no new GitHub permissions, no Telnyx/SendGrid/Vapi
account changes for H5/H6 themselves (staging SMS uses Telnyx trial mode to
one verified test number — see step A), no Stripe changes (leave Stripe
unset in staging), and no production credentials shared with anyone — the
person doing the steps works in the Railway dashboard with their own login.

---

## 2. What was verified in code/docs (no Railway access needed)

Each item states what is verified and what remains unverified without
Railway access. All file paths are relative to the repo root on branch
`topai-harness-critical-fixes` (HEAD `3f51da2`).

### Deployment procedure — VERIFIED IN CODE/DOCS
- `Procfile`: `web: python -m migrations.runner && gunicorn app:app
  --bind 0.0.0.0:$PORT --workers 2 --timeout 120`; `worker: python -m
  workers.sms_campaign_worker`.
- `railway.toml`: Nixpacks builder, `restartPolicyType = "ON_FAILURE"`,
  `restartPolicyMaxRetries = 10`. It defines no services — service
  definitions live in the Railway dashboard, not the repo.
- Assessment plan line 126 and `docs/harness-staging.md` §5: Railway
  auto-deploys `main`; rollback is `git revert <sha>` + push, or a
  Railway redeploy of a previous deployment.
- **Verified by `git cat-file -p`:** deployed commit `1755347` ("Merge
  Phase 1…") has **1 parent** (`7f760b8`); current `origin/main` HEAD
  `5e8e22f` ("Merge critical harness fixes…") has **1 parent**
  (`1755347`). Both are single-parent commits despite "Merge" in their
  messages, so plain `git revert <sha>` is the correct form;
  `git revert -m 1` fails on them. The one true merge (`9e1d2ee`, 2
  parents) is local-only and was never deployed. `docs/harness-staging.md`
  §5 already records this correction.
- **NOT VERIFIED:** that Railway auto-deploy from `main` is actually
  enabled on the production service; that the `7f760b8` deployment is
  still listed/redeployable (R9); any live dashboard state at all.

### Worker service — VERIFIED IN CODE, PRODUCTION WIRING NOT VERIFIED
- `workers/harness_worker.py` exists and implements the full loop:
  `reclaim_stale_jobs()` every iteration, `claim_jobs(limit=10)` with
  Postgres `FOR UPDATE SKIP LOCKED` (see `harness/jobs.py:221-239`),
  dispatch by kind (`sms_auto_reply`, `scheduled_follow_up`,
  `execute_approval`), unknown kinds dead-letter, heartbeat log every 30
  loops, graceful SIGINT/SIGTERM shutdown, non-blocking M4 alert hooks
  on dead-letter / provider failure / worker failure.
- `workers/sms_campaign_worker.py` contains **zero** references to
  `harness` — the production Procfile worker does not process harness
  jobs.
- Nothing in `app.py` or `config.py` starts the harness worker.
- **NOT VERIFIED:** whether a production Railway service running
  `python -m workers.harness_worker` exists (R7). This is the single
  most important dashboard check in this document: if no such service
  exists, production harness jobs sit unprocessed. `docs/harness-staging.md`
  §1 already specified the staging worker start command; that spec is
  now satisfiable because the worker exists (the doc's "once the Phase 1
  worker exists" condition is met — see the update to that file).

### Alerting path — VERIFIED IN CODE + TESTS
- `harness/alerts.py`: `WebhookNotifier` (stdlib `urllib`, POST JSON,
  5 s timeout, non-2xx raises); env switch `HARNESS_ALERT_WEBHOOK_URL`
  (unset = no external send, audit + log only) and `HARNESS_ALERTS_ENABLED`
  (`false`/`0`/`no`/`off` disables); dedup per event key in-memory plus
  an `ops_alert` audit row (provider/worker failures re-alert hourly);
  redaction via `harness.jobs.redact_text(redact_pii=True)` plus a
  secret-only fallback, and payload values reduced to key names only;
  every public function catches its own failures and returns
  `False`/`None` instead of raising, so a failing notifier can never
  break job processing. Threshold: 3 consecutive provider failures
  (`HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD`, default 3).
- Wiring verified: `workers/harness_worker.py` calls
  `alert_dead_letter` / `alert_provider_failure` / `alert_worker_failure`
  on the matching transitions; `harness/policy.py` calls
  `record_spend_alert` (audit + log only, no webhook — spend alerts are
  deliberately not external).
- Tests: `tests/test_harness_alerts.py`, **14 passed** (run 2026-11-08
  on this branch, including webhook-env on/off behaviour).
- **NOT VERIFIED:** no webhook URL is set anywhere in the repo (and none
  should be — secrets do not belong in the repo); no live delivery has
  ever been attempted. Needs R8 + a staging dead-letter test (step F).

### Audit logging — VERIFIED IN CODE
- `harness/audit.py`: `record_audit()` appends one row
  (`user_id, actor, action, tool, target_type, target_id, mode,
  gate_decision, gate_reason, cost_cents, request_id, correlation_id,
  created_at`); the module has **no update/delete functions** by design.
  All queries are `user_id`-scoped. `harness/routes.py::get_harness_status`
  reports `job_counts` including `dead_letter` (M4 follow-up).
- **NOT VERIFIED:** live audit rows in production/staging (needs a
  running service + database access).

### Rollback procedure — VERIFIED IN CODE/DOCS, NEVER EXERCISED LIVE
- `docs/harness-staging.md` §5 (corrected single-parent revert, git
  identity prerequisite, additive tables left in place, flag kill-switches
  `SMS_AI_AUTO_REPLY_ENABLED=false` + `UPDATE harness_settings SET
  paused = 1;`, verification steps). The acceptance test (Test 7)
  proved the reverted tree is byte-identical to pre-Phase-1 `7f760b8`
  and the app boots on it — in a **local SQLite simulation only**.
- Migrations 029/030/031 are additive only (`CREATE TABLE/INDEX IF NOT
  EXISTS`, idempotent seeds); no down-migration exists by design.
- **NOT VERIFIED:** the §5 step 6 requirement ("test this runbook in
  staging first") cannot be followed — no staging exists. Railway
  redeploy-path retention is unknown (R9).

### Staging spec — VERIFIED AS A SPEC, WITH TWO CORRECTIONS APPLIED
- `docs/harness-staging.md` is a complete spec (§1 services, §2 env
  vars, §3 worker, §4 demo, §5 rollback, §6 open items). Two errors
  found during this work and corrected in the same file:
  1. §4 step 1 said the webhook enqueues a job of kind `inbound_sms`.
     The code enqueues kind **`sms_auto_reply`** (`sms_ai_agent.py`,
     idempotency key `sms-auto-reply-<inbound_id>`). No `inbound_sms`
     job kind exists.
  2. The scope note and §1 said the worker is a "later Phase 1 build"
     and "do not assume it exists yet". It now exists
     (`workers/harness_worker.py`), so that condition is met.
- **`HARNESS_MOCK_PROVIDERS=true` does NOT exist in code.** A repo-wide
  grep on this branch returns zero hits in any `.py` file. The staging
  doc §2 already says this ("This flag does not exist in code yet").
  Until it is implemented, the staging demo must run in **Telnyx trial
  mode only** (sends restricted to `TELNYX_VERIFIED_TEST_NUMBER` and no
  other number). Do not assume mock providers are available.
- **NOT VERIFIED:** no staging service, staging database, or staging env
  var exists (spec only, per the doc itself).

---

## 3. Step-by-step instructions (Railway dashboard)

Each step gives the navigation path as best determinable from the repo
docs, the expected outcome, and how to verify it. Paths marked
"(layout NOT VERIFIED)" could not be confirmed against the live
dashboard — if the dashboard looks different, follow the equivalent
screen and note the actual path in the §7 log.

### Step A — Create the staging + staging-worker services

**Do first:** steps A–B before any staging demo; nothing here touches
production services or data.

1. Open [railway.com](https://railway.com) (log in with the account that
   owns `gracious-expression`) → open project **`gracious-expression`**.
2. Create the staging Postgres first (needed for the service variables):
   project canvas → **New → Database → PostgreSQL**. Wait for it to show
   a healthy/running state. Note its service name as shown (expected:
   `Postgres` or similar — record the actual name).
   **Expected outcome:** a second Postgres service alongside production.
   **Verify:** the new Postgres service shows running; its Variables
   tab contains a `DATABASE_URL` that is different from production's.
3. Create the staging web service: **New → GitHub Repo** → select the
   same repository as production → when prompted for a branch, select
   the branch under test (the harness branch, currently
   `topai-harness-critical-fixes`; use `main` only if Ren explicitly
   directs it — staging must never silently track production's
   auto-deploy branch). Name the service **`staging`**.
   - Set its start command to the production web command (Service →
     Settings → Start Command, or the Deploy settings — layout NOT
     VERIFIED):
     `python -m migrations.runner && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
   - If Railway asks for a Root Directory or a custom build, leave the
     defaults that match production (Nixpacks, per `railway.toml`).
4. Create the staging worker service the same way; name it
   **`staging-worker`**; start command:
   `python -m workers.harness_worker`
5. Set variables on **both** `staging` and `staging-worker`
   (Service → Variables), per `docs/harness-staging.md` §2:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | The **staging** Postgres URL (copy from the staging Postgres service Variables, or use Railway's service reference/variable linking if offered — do not type the production URL). |
   | `APP_ENV` | `staging` |
   | `FLASK_SECRET_KEY` | A fresh random secret, different from production (generate one; do not reuse production's). |
   | `ANTHROPIC_API_KEY` | Real key (same account is fine; usage is metered and recorded in `harness_costs`). |
   | `SMS_PROVIDER` | `telnyx` |
   | `TELNYX_TRIAL_MODE` | `true` |
   | `TELNYX_VERIFIED_TEST_NUMBER` | One real mobile number owned by the tester, E.164 (e.g. Ren's). The only number staging may text. |
   | `SMS_AI_AUTO_REPLY_ENABLED` | `false` |
   | `HARNESS_MOCK_PROVIDERS` | **Do NOT set — the flag does not exist in code** (see §2). Trial mode above is the only send control. |
   | `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` | Leave **unset** (staging login then works without billing). |
   | `REGISTRATION_ENABLED` | `false` (default; keep staging closed). |
   | `HARNESS_ALERT_WEBHOOK_URL` | A **test** webhook URL (different channel/endpoint from production), or leave unset for the first run and add it in step F. |

   Do **not** copy production Vapi keys, SendGrid keys, or the production
   Telnyx API key / messaging profile into staging.
   Confirm `ALLOW_DESTRUCTIVE_DB_RESET`, `ALLOW_SQLITE_TABLE_REBUILD`,
   and `RUN_DEMO_SEED_ON_STARTUP` are unset or `false` on both services.
6. Deploy both services (they should deploy on creation; otherwise
   Service → Deployments → Deploy / Redeploy).
   **Expected outcome:** both services show a successful deployment;
   `staging` logs show `engine=postgres`, `postgres_active=true`, and
   migration versions up to 031, with no secret values in the logs.
   **Verify:** open the staging URL in a browser — the login page loads.
   Create the demo tenant directly in the staging DB (see
   `docs/harness-staging.md` §4; the signup page returns 503 without
   Stripe, so create the user with `db.create_user` against the staging
   database or the equivalent — never against production), log in, and
   confirm `harness_settings` for that user is `mode='drafts_only',
   paused=0`. If either service crashes on boot, read its Deploy Logs —
   the usual cause is a missing/wrong variable above.

### Step B — Confirm Postgres backup/snapshot settings and retention

1. Project `gracious-expression` → open the **production** Postgres
   service → look for a **Backups** tab/section (layout NOT VERIFIED —
   if there is no Backups tab, check Settings).
2. Record exactly what is shown: automated backups/snapshots
   enabled or disabled; retention window (days); point-in-time recovery
   available or not; date/time of the most recent snapshot, if any.
3. Repeat for the **staging** Postgres (for the record; staging backups
   are nice-to-have, production is the requirement).
4. **Expected outcome:** a written record in §7 of this file (or in the
   goal file) of the production settings. There is no "wrong" answer
   here — the point is to know.
   **Verify:** you can state, from the dashboard, whether a backup
   exists and how long it is kept. If no Backups option exists at all,
   record "automated backups not available on the current plan" and
   treat the manual `pg_dump` (step C) as the only backup.
   **NOT VERIFIED:** whether Railway's Postgres on this project/plan
   offers automated backups at all; the plan tier is unknown from the
   repo.

### Step C — Take a `pg_dump` before the next deploy

Do this **before every production deploy** until/unless step B shows
automated backups with adequate retention. The first dump also closes
the "no pre-deploy backup evidence" part of H5 for the Nov 8 deploy
(it cannot retroactively create a backup for that deploy — record it
as the first evidenced backup, dated).

1. Production Postgres service → Variables → copy the
   `DATABASE_URL` (or `DATABASE_PUBLIC_URL` if a public URL is shown —
   use the public one from a local machine; the internal
   `*.railway.internal` URL is not reachable from outside Railway).
   Treat the value as a password: paste it only into the local shell
   command below, never into chat, tickets, or files.
2. On a local machine with PostgreSQL client tools installed
   (`pg_dump` available — install via your OS package manager if needed):
   ```bash
   pg_dump "$DATABASE_URL" --format=custom --file="backup-$(date +%Y%m%d-%H%M%S).dump"
   ```
   (Exact command from `docs/DATABASE_SAFETY.md`.) If you use the
   Railway CLI instead (`railway link` to the project, then
   `railway run pg_dump …`), that is equivalent — CLI setup on Ren's
   machine is NOT VERIFIED, so the dashboard-copy route above is
   primary.
3. Record in §7: date/time, file name, file size, SHA-256
   (`shasum -a 256 backup-*.dump`), and where the file is stored
   (local secure storage or object storage — outside the app container).
   **Expected outcome:** a non-empty `.dump` file (size > 0; a
   near-empty file means the dump failed or hit the wrong database —
   check with `pg_restore --list` showing table entries).
   **Verify:** `pg_restore --list backup-<...>.dump | head` lists the
   app tables (e.g. `users`, `harness_settings`). Do **not** restore
   it anywhere yet — that is step D.

### Step D — Exercise a restore on a scratch Postgres

1. Provision a scratch target. Pick one:
   - **Preferred:** project `gracious-expression` → New → Database →
     PostgreSQL (name it `scratch-restore` or similar; delete it
     afterwards), or
   - a local Postgres database created empty for this purpose.
   Do **not** use the staging Postgres unless it holds nothing worth
   keeping — `pg_restore --clean` replaces the target's contents.
2. Get the scratch database's connection URL from its Variables tab
   (same handling as step C).
3. Run:
   ```bash
   pg_restore --clean --if-exists --no-owner --dbname="<scratch DATABASE_URL>" backup-<YYYYMMDD-HHMMSS>.dump
   ```
   (Exact command from `docs/DATABASE_SAFETY.md`.)
4. Spot-check the restored database: connect with `psql` (or the
   Railway Postgres query view, if offered) and compare row counts
   against production for a few tables, e.g.
   `SELECT count(*) FROM users;`, `SELECT count(*) FROM harness_settings;`,
   `SELECT count(*) FROM harness_jobs;`. Counts should match the
   production counts at dump time.
5. **Expected outcome:** `pg_restore` exits 0 (warnings about
   already-existing extensions are acceptable; errors about missing
   tables are not) and row counts match.
   **Verify:** record the restore date, target, exit status, and the
   compared counts in §7. Then delete the scratch Postgres service
   (project → scratch service → Settings → Delete) if one was created,
   so it is not mistaken for staging later.
   **NOT VERIFIED:** no restore has ever been attempted; the exact
   `pg_restore` behaviour against this database (extensions, ownership)
   is unknown until this step runs.

### Step E — Confirm the pre-Phase-1 deployment is still redeployable

1. Project `gracious-expression` → production **`web`** service →
   **Deployments**.
2. Find the deployment for commit `7f760b8` ("Fix /generate-script
   crash…", 2026-11-07) — or, failing that, the deployment immediately
   preceding the Phase 1 deployment `1755347` (2026-11-08). Note its
   date, status, and whether a **Redeploy** action is offered.
   (Deployment-list layout and retention NOT VERIFIED — if the list
   only goes back a few days, the deployment may already be gone.)
3. **Do not click Redeploy on production in this step** — this step is
   a read-only confirmation that the option exists.
4. **Expected outcome:** the `7f760b8` deployment is listed with a
   working Redeploy action, and the retention limit is noted (e.g.
   "deployments kept for N days" if the dashboard states it).
   **Verify / record in §7:** listed yes/no, Redeploy offered yes/no,
   retention limit if shown. If it is gone, record "expired — `git
   revert` + push is the only rollback path" and H6 stays conditional
   on the revert path only.

### Step F — Set the alert webhook URL

1. Create the receiving webhook first, in the receiving app:
   - **Slack (expected):** Slack workspace → the channel for TopAI ops
     alerts → Integrations / Add apps → Incoming Webhooks → Add to
     Slack → copy the `https://hooks.slack.com/services/…` URL.
     (Exact Slack navigation NOT VERIFIED — Slack's UI changes; any
     HTTPS endpoint that accepts a POST JSON body works.)
   - Keep the production URL and the staging test URL separate (two
     webhooks / two channels, or one channel with the staging one
     clearly labelled in tests only).
2. Railway → production service(s) running harness code → Variables →
   set `HARNESS_ALERT_WEBHOOK_URL` to the production webhook URL.
   Set it on the service that runs the harness worker (R7) and on
   `web`. Leave `HARNESS_ALERTS_ENABLED` unset (enabled by default).
   Redeploy the affected service(s) if Railway does not redeploy
   automatically on variable change (expected: Railway redeploys on
   variable change — NOT VERIFIED on this project).
3. **Verify in staging first:** set the test webhook URL on
   `staging`/`staging-worker`, then force a dead-letter in staging
   (e.g. enqueue a job with an unknown kind, or stop the staging
   worker mid-job repeatedly until a job dead-letters — see the
   staging demo, step G). **Expected outcome:** exactly one alert
   POST arrives at the test endpoint for that dead-letter, its JSON
   contains `event: "dead_letter"` and no secret/phone/email values,
   and a matching `ops_alert` row appears in staging `harness_audit`.
   A second identical dead-letter must not re-alert (dedup).
4. Only after the staging verification passes, repeat step 2's
   production setting (if not already set) and record both URLs'
   existence (never the URL values) in §7.
   **NOT VERIFIED:** no webhook URL exists today; no alert has ever
   been delivered. The code path is tested (14 tests) but live
   delivery is not.

### Step G — Run the staging demo + rollback demo

Run `docs/harness-staging.md` §4 (end-to-end demo) and §5 (rollback
demo) in staging, with these corrections applied:

- Demo job kind is **`sms_auto_reply`**, not `inbound_sms` (§4 step 1
  as corrected).
- **No mock providers**: `HARNESS_MOCK_PROVIDERS` does not exist in
  code. The demo runs in Telnyx trial mode only; every send goes to
  `TELNYX_VERIFIED_TEST_NUMBER` and no other number.
- The harness worker exists, so `staging-worker` runs
  `python -m workers.harness_worker` as specified.
- Approval step (§4 step 4): approve in the staging UI at `/harness`
  (the "AI Automation" page) — the approval API + worker execution
  path now exists (`harness/routes.py`, `harness/approvals.py`).
  Marking the DB row directly is a fallback only.

**§4 demo pass criteria** (from the staging doc): exactly one send per
approved draft, zero sends in Drafts Only without approval, cost rows
matching the demo's Anthropic calls, a killed worker losing no job,
prompt-injection message producing no tool call and one audit row,
paused tenant producing draft only.

**§5 rollback demo (staging only):**
1. On a staging branch (never on `main`), `git revert <staging
   deployed commit>` (plain revert — the deployed commits are
   single-parent; see §2), push, let staging redeploy.
2. Confirm staging serves pre-Phase-1 behaviour (no AI Automation
   section, `GET /api/harness/status` → 404, inbound SMS on the old
   daemon-thread path), no new `harness_jobs` rows appear, and the
   §4 demo now fails safe.
3. Restore staging to the harness branch afterwards and re-run §4.
   **Expected outcome:** rollback demonstrated end-to-end in staging
   with dates/results recorded in §7. This is the step that closes H6;
   the local SQLite simulation from the acceptance test does not.

---

## 4. Items Ren must provide or do (summary)

1. **Railway dashboard access** to project `gracious-expression` at a
   level that can create services, edit variables, and deploy/redeploy
   (R1–R3, R7–R9). If Ren does not hold that access, name the person
   who does.
2. **A verified test mobile number** for `TELNYX_VERIFIED_TEST_NUMBER`
   (step A).
3. **A fresh `FLASK_SECRET_KEY` for staging** and confirmation of the
   **Anthropic key** to use in staging (step A).
4. **A Slack incoming-webhook URL** (or another HTTPS POST endpoint)
   for `HARNESS_ALERT_WEBHOOK_URL`, production and staging separately
   (step F).
5. **Decisions:** (a) which branch staging deploys (harness branch vs
   `main`); (b) whether a production harness-worker service should
   exist at all — the assessment plan gated it as a Phase 1 decision
   (R7); (c) if step B shows no automated backups, accept `pg_dump`
   as the backup method and its cadence.
6. **Execution of steps A–G** in the dashboard, or delegation to
   someone with access, with results recorded in §7.

---

## 5. What is still NOT VERIFIED (do not mark H5/H6 closed until these clear)

- [ ] R1/R2/R3: staging services + staging Postgres created.
- [ ] R4: production Postgres backup/snapshot settings + retention
      observed in the dashboard.
- [ ] R5: first evidenced pre-deploy `pg_dump` taken and recorded
      (date, size, SHA-256, location).
- [ ] R6: restore exercised on a scratch Postgres with matching
      row counts.
- [ ] R7: production harness-worker service existence confirmed
      (or its absence recorded with the consequence stated).
- [ ] R8: alert webhook URL set and a live dead-letter alert
      delivered in staging (and an `ops_alert` audit row written).
- [ ] R9: pre-Phase-1 deployment (`7f760b8`) listed and redeployable,
      retention noted.
- [ ] Step G: staging §4 demo passed and §5 rollback demo passed,
      with dates and results recorded.

---

## 6. Related docs

- `docs/harness-staging.md` — staging spec, §4 demo, §5 rollback
  runbook, §6 open items (updated 2026-11-08 for the corrections in
  §2 above).
- `docs/DATABASE_SAFETY.md` — production Postgres architecture,
  pre-deployment backup (`pg_dump`) and restore (`pg_restore`)
  commands.
- `~/workspace/goals/topai-real-estate-tools-maintenance/files/AI_Harness_Phase1_Acceptance_Report.md`
  — Test 7 (H5/H6 evidence), deficiency register, re-test addendum.
- `harness/alerts.py`, `harness/audit.py`, `harness/jobs.py`,
  `workers/harness_worker.py` — code verified in §2.

---

## 7. Completion log (fill in as steps are done)

| Date | Step | Observed result | Verified by |
|---|---|---|---|
| — | — | _Nothing recorded yet. No Railway-dashboard item has been observed._ | — |
