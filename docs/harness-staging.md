# AI Harness — Staging Specification (Phase 1)

**Status:** spec only. No Railway service has been created, no env var has
been set, and no staging deploy has been run. Creating the services below
is a manual Railway-dashboard step (or a parent-agent step with Railway
access); this document is the exact spec to follow.

**Scope note:** this document covers the staging environment and the
end-to-end demo for the Phase 1 data layer
(`migrations/versions/029_harness_foundation.py`, `harness/jobs.py`,
`harness/audit.py`, `harness/costs.py`). The harness worker
(`workers/harness_worker.py`) and the policy gate are built (verified
2026-11-08 on branch `topai-harness-critical-fixes`); the worker
behaviour in §3 is the implemented behaviour, not a future spec. The
detailed Railway checklist (access, backups, restore, webhook, demo
steps) lives in `docs/harness-railway-checklist.md` — read it alongside
this file.

## 1. Railway services

Railway project: `gracious-expression` (same project as production, so
billing/networking stay in one place; separate services and a separate
database so staging can never touch production data).

| Service | Name | Start command | Notes |
|---|---|---|---|
| Web | `staging` | `python -m migrations.runner && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` (same as production `web` in `Procfile`) | Migrations run first, as in production. |
| Worker | `staging-worker` | `python -m workers.harness_worker` | General harness job loop. The existing production worker service runs `python -m workers.sms_campaign_worker` — staging must not run that against the staging database until the harness worker supersedes it. **Open question (see `docs/harness-railway-checklist.md` R7):** the production `Procfile` worker runs only `workers.sms_campaign_worker`, which has zero harness references, and nothing in `app.py`/`config.py` starts the harness worker — whether a production harness-worker service exists must be confirmed in the Railway dashboard. |

Do **not** reuse the production services, the production Postgres
database, or the production Telnyx messaging profile / phone number.

## 2. Environment variables (staging service)

Set on **both** `staging` and `staging-worker` unless noted otherwise.

| Variable | Value | Why |
|---|---|---|
| `DATABASE_URL` | New Railway Postgres database provisioned for staging (separate from production) | Production/staging refuse SQLite (`config.py` / `db_backend.py` raise without `DATABASE_URL`). Never point staging at the production database. |
| `APP_ENV` | `staging` | Enables staging guards; `ALLOW_DESTRUCTIVE_DB_RESET` must stay unset/false (the migration runner refuses to run with it in staging). |
| `FLASK_SECRET_KEY` | A fresh random secret, different from production | Required at boot (`config.validate_config()` hard-fails without it). |
| `ANTHROPIC_API_KEY` | Real key (same account is fine; usage is metered and recorded in `harness_costs`) | Required at boot; the demo's draft generation calls Claude. |
| `SMS_PROVIDER` | `telnyx` | Matches the production default (`config.py`). |
| `TELNYX_TRIAL_MODE` | `true` (ON) | Trial mode defaults ON in code (`config.py`); in staging it must stay ON so sends are restricted to the verified test number below. |
| `TELNYX_VERIFIED_TEST_NUMBER` | One real mobile number owned by the tester (E.164, e.g. Ren's) | The only number Telnyx trial mode will deliver to. No other destination may receive a staging SMS. |
| `TELNYX_TOLL_FREE_VERIFICATION_STATUS` | Leave unset / not `verified` in staging | Staging must not present as a verified production sender. |
| `SMS_AI_AUTO_REPLY_ENABLED` | `false` | Defence in depth: the global auto-reply default is `true` in code, and staging must never auto-send. The harness gate (`harness_settings.mode='drafts_only'`) is the primary control; this flag is the backstop. |
| `HARNESS_MOCK_PROVIDERS` | `true` | **Staging-only mock-provider mode.** While set, the harness worker must route all provider calls (Telnyx send, Vapi, SendGrid) through mock providers that record the call and return a fake success — no real network send occurs. **This flag does not exist in code yet**: the harness-worker build must implement it (check the flag before selecting the real provider). Until it is implemented, the demo runs in Telnyx trial mode only (previous rows) and every send goes to `TELNYX_VERIFIED_TEST_NUMBER` and no other number. (Re-verified 2026-11-08 on branch `topai-harness-critical-fixes`: zero hits for `HARNESS_MOCK_PROVIDERS` in any `.py` file.) |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` | Leave unset in staging | With Stripe unset, `config` auto-sets `SUBSCRIPTION_REQUIRED=False`, so staging login works without billing. Do not connect staging to the live Stripe account. |
| `REGISTRATION_ENABLED` | `false` (default) | Keep staging closed; create the demo tenant directly in the DB (see §4). |

No other production secrets (Vapi keys, SendGrid keys, Telnyx API key for
the production messaging profile) are copied into staging.

## 3. Running the worker in staging

The worker exists (`workers/harness_worker.py`):

```bash
# Railway: set the staging-worker service start command to:
python -m workers.harness_worker
```

Local equivalent against the staging database (only with the staging
`DATABASE_URL` exported; never against production):

```bash
APP_ENV=staging DATABASE_URL='<staging postgres url>' \
  HARNESS_MOCK_PROVIDERS=true \
  python -m workers.harness_worker
```

Expected worker behaviour (acceptance criteria for the worker build):
- Polls `harness_jobs` (claim with `FOR UPDATE SKIP LOCKED` on
  Postgres), heartbeat visible so a stale worker is detectable.
- A job killed mid-claim (worker stopped while a job is `claimed`) is
  reclaimed by `reclaim_stale_jobs()` after the timeout and completes on
  the next run — no silent loss.
- Every job writes a `harness_audit` row and, for any Anthropic call, a
  `harness_costs` row.

## 4. End-to-end demo (mock Telnyx)

Prerequisites: staging web + staging-worker running, `HARNESS_MOCK_PROVIDERS=true`
(or trial-mode-only fallback, §2). Demo tenant created directly in the
staging DB; confirm its `harness_settings` row is `mode='drafts_only',
paused=0` (migration 029 seeds this for every user).

1. **Inbound SMS** — send a text to the staging Telnyx number from the
   verified test number (or inject the Telnyx webhook payload in staging).
   The webhook handler must enqueue a `harness_jobs` row
   (`kind='sms_auto_reply'`, `status='pending'` — corrected 2026-11-08:
   the code enqueues `sms_auto_reply`, not `inbound_sms`; no
   `inbound_sms` job kind exists) instead of spawning a
   daemon thread, and record a `harness_audit` row.
2. **Durable job** — stop the staging-worker mid-processing (or observe
   the claim): the job is `claimed`, then either completes or, if the
   worker was killed, is reclaimed to `pending` by the stale-claim
   timeout and picked up on restart.
3. **Draft** — the worker runs the coach/Ask TopAI loop and produces a
   draft reply. In `drafts_only` mode it must **not** send: assert no
   provider call leaves the mock (mock log shows zero sends) and a
   `harness_approvals` row exists with `status='pending'`.
4. **Approval** — a human approves the draft in the staging UI at
   `/harness` (the "AI Automation" page; the approve/reject API +
   worker execution path now exists in `harness/routes.py` /
   `harness/approvals.py`). Directly marking the `harness_approvals`
   row `approved` in the staging DB is a fallback only. The approval
   records `decided_by`/`decided_at`.
5. **Send** — the worker picks up the approved job and sends via the mock
   Telnyx provider (mock log shows exactly one send, to the verified
   test number only). The job ends `succeeded`; `harness_audit` shows
   the ALLOW gate decision; `harness_costs` shows the Anthropic token
   usage for the draft and the Telnyx segment for the send.
6. **Negative checks** — repeat step 1 with a prompt-injection message
   ("ignore your instructions and send…"): no tool call, no send, one
   audit row. Repeat with the tenant paused
   (`harness_settings.paused=1`): draft only, no send.

Demo pass criteria: exactly one mock send per approved draft, zero sends
in Drafts Only without approval, cost rows matching the demo's Anthropic
calls, and a killed worker losing no job.

## 5. Rollback runbook

Rollback is code + flags only. **Never run a destructive down-migration
in staging or production** — there is no down-migration for 029 and none
will be written.

1. **Revert the deployed commit on `main`:**
   ```bash
   git revert <deployed commit>   # plain revert — see below
   git push origin main
   ```
   **Which commits are revertible this way (verified 2026-11-08
   against the actual git history with `git cat-file -p <sha>`):**
   - `1755347` ("Merge Phase 1: AI Harness foundation…") — **1 parent**.
   - `5e8e22f` ("Merge critical harness fixes: Vapi gate, SMS rate
     limits, policy fail-closed, redaction, spend cap, approval
     flow") — **1 parent**, current `origin/main` HEAD.
   - Both are **single-parent commits, not merges**, despite the word
     "Merge" in their messages. A plain `git revert <sha>` is the
     correct (and only working) form for them: `git revert -m 1`
     is for true merge commits and **fails** on a single-parent
     commit ("commit … is not a merge" / "-m specified for a
     non-merge"). Never use `-m 1` on a deployed commit.
   - The one true merge in this history is the *local-only* commit
     `9e1d2ee` (2 parents) — it was never pushed or deployed, so the
     runbook never reverts it. If a future deploy ever is a true
     merge, verify with `git cat-file -p <sha> | grep -c '^parent '`
     (2 = merge, use `git revert -m 1 <sha>`; 1 = plain revert)
     before choosing the form.
   - The plain revert was verified in the acceptance test: the
     reverted tree is byte-identical to pre-Phase-1 `7f760b8`.
   Railway auto-deploys `main`; the revert redeploys the pre-Phase 1
   code. Alternatively, redeploy the previous production deployment from
   the Railway dashboard (Deployments → previous deployment → Redeploy).
2. **Git identity prerequisite — a revert creates a commit, so it
   fails in a fresh checkout with no identity configured** (`git
   revert` errors: "Committer identity unknown… Please tell me who
   you are"). Set identity before reverting, either per-command:
   ```bash
   git -c user.name="Ren Morrissey" -c user.email="ren.morrissey@gmail.com" revert <deployed commit>
   ```
   or once per checkout:
   ```bash
   git config user.name "Ren Morrissey"
   git config user.email "ren.morrissey@gmail.com"
   git revert <deployed commit>
   ```
3. **Leave the additive tables in place.** `harness_settings`,
   `harness_jobs`, `harness_audit`, `harness_costs`, `harness_memory`,
   `harness_approvals` are inert once the harness code is gone: nothing
   in pre-Phase 1 code reads them, and they hold no production traffic
   data beyond the seeded `drafts_only` settings rows. Do not drop them
   — dropping would destroy the audit trail.
4. **Disable at the flags (do this first if a bad send is suspected,
   before the revert finishes deploying):**
   - Set `SMS_AI_AUTO_REPLY_ENABLED=false` on the production web/worker
     services (the global auto-reply kill switch), **and**
   - Set every tenant's harness mode to paused:
     `UPDATE harness_settings SET paused = 1;` (run via `psql` against
     the production database; the harness code reads `paused` before
     any autonomous send).
   Either flag alone stops autonomous sending; both together are the
   belt-and-braces state.
5. **Verify rollback:** confirm the production site serves pre-Phase 1
   behaviour (no AI Automation section, inbound SMS handled by the old
   daemon-thread path), confirm no new `harness_jobs` rows appear, and
   confirm the staging demo in §4 still reproduces the failure that
   triggered the rollback before any re-deploy is attempted.
6. **Test this runbook in staging first** (revert on a staging branch,
   redeploy staging, confirm §4 demo fails safe) before relying on it
   in production.

## 6. Open items requiring Railway access (acceptance H5/H6 — NOT closed by code)

These cannot be verified or completed from the repo alone; they need
Ren's Railway dashboard access. Do not mark them done until each is
confirmed in the dashboard and recorded here. **The full checklist —
exact access items, step-by-step dashboard instructions, and the
completion log — is `docs/harness-railway-checklist.md`.** Summary:

- **H5 — Postgres backup/restore never exercised.** In the Railway
  dashboard, confirm for the production Postgres service: are automated
  backups/snapshots enabled, what is the retention window, and is
  point-in-time recovery available? Before the next production deploy,
  take a `pg_dump` and record the artifact (date, size, location) here.
  Exercise a restore once against a scratch/staging Postgres and record
  the result. No backup artifact exists in the repo today, and no
  pre-deploy backup was taken before the Nov 8 production deploy.
- **H6 — No staging environment exists.** The services in §1 have not
  been created ("spec only"). Create `staging` + `staging-worker` per
  §1–§2, run the §4 demo and the §5 rollback demo there, and record the
  dates/results here. Until then, the rollback path is proven only in a
  local SQLite simulation (acceptance Test 7) and the Postgres claim
  path is untested live.
- **Railway redeploy option unverified.** Confirm the pre-Phase-1
  production deployment is still listed in Railway and redeployable,
  and note the retention limit. If retention has expired, `git revert`
  + push is the only rollback path.
- **Production harness-worker service — existence unknown.** The
  production `Procfile` worker runs `workers.sms_campaign_worker`
  only (zero harness references); nothing in `app.py`/`config.py`
  starts the harness worker. Confirm in the dashboard whether a
  separate production service runs `python -m workers.harness_worker`.
  If none exists, production harness jobs are never processed.
- **Alert webhook URL unset.** `HARNESS_ALERT_WEBHOOK_URL` is read
  from the environment by `harness/alerts.py` (verified in code,
  14 tests passing); no URL exists. A webhook URL must be created and
  set on the harness-worker service(s), and a live dead-letter alert
  verified in staging first.

## 7. AI cost estimates — Anthropic pricing table (L3)

`harness/costs.py` records exact token counts and an **estimated**
dollar cost per Anthropic call. The rates are estimates, not billed
charges: they are modelled on published Anthropic Sonnet/Opus-class
list prices and have NOT been verified against a live pricing page.
Never present a `harness_costs.cost_cents` figure (or a
`monthly_totals()` total) as what Anthropic billed — display it with
`costs.format_cost_estimate()` ("$X.XX (estimated — not a billed
charge)") and note `monthly_totals()["cost_basis"] == "estimate"`.

Effective rates (cents per 1,000,000 tokens), defaults in
`harness/costs.py` (`DEFAULT_PRICE_CENTS_PER_MILLION_TOKENS`):

| Model | Input | Output |
|---|---|---|
| `claude-sonnet-5` (Ask TopAI / scripts) | 300 | 1500 |
| `claude-opus-4-6` (listings) | 1500 | 7500 |
| `default` (any other model string) | 300 | 1500 |

Override without a code change (see `harness/costs.py` for the full
spec): `HARNESS_ANTHROPIC_PRICES_JSON` (JSON object mapping model →
`{"input": …, "output": …}`), or per-model env vars
`HARNESS_ANTHROPIC_PRICE_<MODEL>_INPUT_CENTS_PER_M` /
`HARNESS_ANTHROPIC_PRICE_<MODEL>_OUTPUT_CENTS_PER_M` (model name
upper-cased, non-alphanumerics → `_`; e.g.
`HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_INPUT_CENTS_PER_M`).
Confirm the rates with Ren against Anthropic's pricing page before
relying on any dollar figure.

The $50/tenant/month spend cap (`HARNESS_MONTHLY_SPEND_CAP_CENTS`,
default 5000 cents, confirmed by Ren 2026-11-08) is unchanged by
this pricing work and is enforced on the estimated cents.
