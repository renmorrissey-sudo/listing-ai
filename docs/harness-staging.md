# AI Harness — Staging Specification (Phase 1)

**Status:** spec only for the Railway side. No Railway service has been
created, no env var has been set, and no staging deploy has been run.
Creating the services below is a manual Railway-dashboard step (or a
parent-agent step with Railway access); this document is the exact spec
to follow. **Code side re-verified 2026-11-08** on branch
`launch-staging-verify` (38 staging-safety tests, 5 holes found and
fixed — see §2a): the guards below are proven against hostile staging
configs, including the worker processes and the approval carve-out
executing through the mock provider. What remains is purely the
Railway-dashboard creation in §10 — no further code is needed to
create staging.

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
| Worker | `staging-worker` | `python -m workers.harness_worker` | General harness job loop. The existing production worker service runs `python -m workers.sms_campaign_worker` — staging must not run that against the staging database until the harness worker supersedes it. **Open question (see `docs/harness-railway-checklist.md` R7):** the production `Procfile` worker runs only `workers.sms_campaign_worker`, which has zero harness references, and nothing in `app.py`/`config.py` starts the harness worker — whether a production harness-worker service exists must be confirmed in the Railway dashboard. Production service spec (Procfile `harness-worker` process, env vars, heartbeat, restart/recovery): `docs/harness-worker-architecture.md`. |

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
| `TELNYX_TRIAL_MODE` | `true` (ON) | Trial mode defaults ON in code (`config.py`); in staging it must stay ON so sends are restricted to the verified test number below. **Fallback only** while the mock is on: with mock providers enabled, no Telnyx call is ever made. |
| `TELNYX_VERIFIED_TEST_NUMBER` | One real mobile number owned by the tester (E.164, e.g. Ren's) | The only number Telnyx trial mode will deliver to. No other destination may receive a staging SMS. Required by the boot guard whenever trial mode is the active send control (see §2a). |
| `TELNYX_TOLL_FREE_VERIFICATION_STATUS` | Leave unset / not `verified` in staging | Staging must not present as a verified production sender. |
| `SMS_AI_AUTO_REPLY_ENABLED` | Leave **unset** (default `true`) | **Corrected 2026-11-08 (re-verification hole W5):** the old value `false` broke the §4 demo — `process_inbound_ai` returns `auto_reply_disabled` even for a human-approved execution, so an approved draft could never send, even via the mock. The flag must stay on for approved executions to send at all. It is NOT a safety control in staging: unapproved sends are stopped by the harness drafts_only lock (G2), and every live inbound flow goes through the harness job queue, never the legacy direct auto-reply path. Regression test: `test_staging_auto_reply_flag_off_blocks_approved_execution`. |
| `HARNESS_MOCK_PROVIDERS` | `true` | **Implemented 2026-11-08 on branch `topai-harness-hardening-staging`** (`config.py`, `sms_providers/mock.py`, `sms_providers/factory.py`): the SMS provider factory returns the mock provider, which records sends in memory and never touches the network. Defaults ON in staging and OFF elsewhere, so an explicit `true` here is belt-and-braces. All SMS sends (harness worker, SMS campaign worker, `sms_ai_agent`) funnel through this factory, so the mock intercepts every send path. The staging demo asserts on the recorded sends (`sms_providers.mock.sent_messages()`). |
| `HARNESS_MONTHLY_SPEND_CAP_CENTS` | `500` ($5) | Lower than the production $50 default (5000): bounds Anthropic spend for the staging demo. The cap is enforced by the policy gate (`harness/policy.py`). |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` | Leave unset in staging | Do not connect staging to the live Stripe account. A live (`sk_live_…`) key in staging is refused at boot by `config.validate_staging_safety()`. Note: with Stripe unset, staging does **not** get the dev/test `SUBSCRIPTION_REQUIRED=False` shortcut (`config.py` applies it only to `development`/`test`), so signup stays gated and the demo tenant is created directly in the DB (see §4). |
| `SENDGRID_API_KEY` / `SMTP_HOST` | Leave unset in staging | **Refused at boot** since the 2026-11-08 re-verification (hole W3): a SendGrid key or SMTP config made real email reachable from the staging UI (password reset) with only a stderr warning. There is no SendGrid test key; email stays unset until an email mock exists. |
| `VOICE_PROVIDER_API_KEY` (Vapi) | Leave unset in staging | **Refused at boot** since the 2026-11-08 re-verification (hole W4): the key lets any logged-in staging user place a real outbound call via `POST /voice/calls`, outside the harness gate. Voice stays unset until a voice mock exists. |
| `OPENAI_API_KEY` | Leave unset in staging | **Refused at boot** since the 2026-11-08 re-verification (hole W4): the key lets any logged-in staging user mint a real metered realtime session via `POST /api/ask-topai/live/session`, outside the harness gate and outside the harness spend cap. |
| `REGISTRATION_ENABLED` | `false` (default) | Keep staging closed; create the demo tenant directly in the DB (see §4). |

No other production secrets (Vapi keys, SendGrid keys, Telnyx API key for
the production messaging profile) are copied into staging.

## 2a. Code-level staging safety guards (implemented 2026-11-08)

Branch `topai-harness-hardening-staging`; tests in
`tests/test_harness_staging_safety.py` — **38 tests** after the
2026-11-08 re-verification on branch `launch-staging-verify` (was 21;
the re-verification found and fixed 5 holes, W1–W5, each with
regression tests — see the probe header in the test file). These
guards are in the code, so they protect any staging deploy regardless
of who sets the variables. They do **not** replace the dashboard
steps in §1–§2 — staging still has to be created.

| # | Guard | Where | What it does |
|---|---|---|---|
| G1 | Mock SMS provider | `sms_providers/mock.py`, `sms_providers/factory.py`, `config.HARNESS_MOCK_PROVIDERS` | When the flag is on (default ON in staging, OFF elsewhere), `get_sms_provider()` returns `MockSMSProvider`, which records the send in memory (`sent_messages()`) and returns a fake `mock-…` message id. No network call, no real delivery. Every SMS send path funnels through this factory (`sms_ai_agent`, `sms_outbound`, both workers), so the mock intercepts all of them. |
| G2 | Staging drafts_only lock | `harness/policy.py` (`get_settings`, `decide`), `harness/sms_handler.py` (`get_harness_mode` fallback) | In staging, every tenant's reported mode is `drafts_only`, whatever is stored in `harness_settings` or passed in a decision context. The stored row is untouched. The sole carve-out is a human approval executing (`harness.approvals` sets `context["human_approval"]=True`): the approval itself satisfies the mode gate, but consent / suppression / quiet-hours / rate-limit / pause are still re-checked, and the send still goes through the mock provider in staging. Supervised/authorized behaviour is unit-tested, never live-tested in staging. |
| G3 | Boot-time staging safety | `config.validate_staging_safety()`, called from `validate_config()` (runs in the web process at import and therefore on every boot; **since the 2026-11-08 re-verification, also called at the start of `main()` in both `workers/harness_worker.py` and `workers/sms_campaign_worker.py` — hole W1: previously only the web process ran it, so a worker could boot in staging with a live Stripe key and process jobs**) | Refuses to boot staging when: a live Stripe secret (`sk_live_…`) is set; `TELNYX_PHONE_NUMBER` is the production number `+18888210810`; a real Telnyx key is set with mock providers off **and** trial mode off; or trial mode is on with no `TELNYX_VERIFIED_TEST_NUMBER`. **Since the re-verification it also refuses:** real Twilio credentials when `SMS_PROVIDER=twilio` and mock off, and a real SimpleTexting token when `SMS_PROVIDER=simpletexting` and mock off (hole W2 — Telnyx trial mode does not restrict those providers, so there is no safe non-mock config for them in staging); any `SENDGRID_API_KEY` or `SMTP_HOST` (hole W3 — was warning-only; real email was reachable from the staging UI); any `VOICE_PROVIDER_API_KEY` (Vapi) or `OPENAI_API_KEY` (hole W4 — real outbound calls / metered realtime sessions outside the harness gate). Keys for an unselected provider, and any provider key while the mock is on, are inert and do not trip the guard. |
| G5 | Mock skips Telnyx delivery checks | `sms_ai_agent._auto_reply_allowed()` | When `HARNESS_MOCK_PROVIDERS` is on, the Telnyx toll-free-verification and trial-destination checks are skipped: the mock never delivers, so those restrictions are meaningless — and enforcing them made an approved mock send unreachable in staging, where the toll-free status is deliberately unset (hole W5). Consent / opt-out / suppression / quiet-hours / rate-limit / escalation checks still apply in full, so the skip cannot produce a real delivery. |
| G4 | Pre-existing backstops (unchanged) | `harness/policy.py`, `config.py`, `db_backend.py` | New tenants default to `drafts_only` (migration 029 seed + `get_settings`); the harness-wide `paused` flag drafts everything; the $50/tenant/month spend cap (500 cents in staging per §2) blocks sends at the cap; production/staging refuse SQLite and refuse destructive flags (`ALLOW_DESTRUCTIVE_DB_RESET`, `ALLOW_SQLITE_TABLE_REBUILD`, `RUN_DEMO_SEED_ON_STARTUP`). |

What the guards deliberately do **not** cover (still manual / dashboard,
or known residual risk):
- **Anthropic spend outside the harness cap (residual risk, NOT fixed
  here).** The $5 staging cap (`HARNESS_MONTHLY_SPEND_CAP_CENTS=500`)
  is enforced only by the harness policy gate. Ask TopAI, listing
  generation, and the legacy SMS-coach path call Anthropic without
  checking the cap — no spend-cap reference exists outside `harness/`
  in the app code — so a staging user driving those surfaces can spend
  real Anthropic dollars beyond the $5 cap. Keep staging demo traffic
  to the harness paths, and watch Anthropic usage during the demo.
  Universal cost recording is a larger change (flagged in the Phase I
  hardening report) and is out of scope for this blocker.
- **Per-user credentials stored in the database.** The boot guard
  inspects environment variables only. `lead_email_service` sends via
  per-user SendGrid credentials stored in `email_marketing_db`; a
  fresh staging database has none, and staging must never be loaded
  with production data — if a staging tenant ever connects SendGrid
  in the UI, that tenant can send real email. Do not connect SendGrid
  in staging.
- **Voice and email have no mock provider** — staging leaves Vapi keys,
  SendGrid keys, and SMTP unset, and the boot guard now *refuses* them
  instead of warning. If a future staging test needs voice or email,
  add a mock for that channel first; do not paste a production key
  into staging.
- **Legacy direct auto-reply path (dead in production, still in
  code).** `sms_inbound.process_inbound_sms(defer_coach=False)` and
  `_schedule_coach(app=None)` call `process_inbound_ai` directly,
  bypassing the harness job queue. No live caller uses either form
  (the webhooks pass `defer_coach=True, app=app`; the workers use the
  harness queue), but the code path exists — do not wire a new caller
  to it.

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
  Postgres), durable heartbeat row in `sms_worker_heartbeats`
  (worker_id `harness-<hostname>-<pid>`, written every loop) surfaced
  in `GET /api/harness/status` as `worker.running`, so a stale or
  missing worker is detectable by query, not just by log grep.
- A job killed mid-claim (worker stopped while a job is `claimed`) is
  reclaimed by `reclaim_stale_jobs()` after the timeout and completes on
  the next run — no silent loss.
- Every job writes a `harness_audit` row and, for any Anthropic call, a
  `harness_costs` row.

## 4. End-to-end demo (mock Telnyx)

Prerequisites: staging web + staging-worker running, `HARNESS_MOCK_PROVIDERS=true`
(mock is the default in staging — see §2a G1; trial mode is the fallback
only if the mock is deliberately switched off). Demo tenant created
directly in the staging DB; confirm its `harness_settings` row is
`mode='drafts_only', paused=0` (migration 029 seeds this for every user;
the staging mode lock in §2a G2 also forces `drafts_only` in code).

1. **Inbound SMS** — inject the Telnyx webhook payload in staging (with
   the mock on there is no live staging Telnyx number to text; if the
   mock is deliberately off and trial mode is on, a text to the staging
   number from the verified test number works instead).
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
   provider (`sms_providers.mock.sent_messages()` shows exactly one
   send; with the mock on, the destination is recorded only — nothing
   is delivered to any real number). The job ends `succeeded`;
   `harness_audit` shows the ALLOW gate decision; `harness_costs` shows
   the Anthropic token usage for the draft. (No Telnyx segment/cost row
   is expected for a mock send.) **Note (2026-11-08 re-verification,
   hole W5):** this step previously could never pass — the old staging
   value `SMS_AI_AUTO_REPLY_ENABLED=false` made the approved send
   refuse with `auto_reply_disabled`, and the unset toll-free status
   made it refuse with `toll_free_not_verified`. Both are fixed: the
   flag now stays at its default (`true`, §2) and the mock skips the
   Telnyx delivery checks (§2a G5). The verified test number need not
   equal the lead's number while the mock is on. Regression test:
   `test_staging_approved_execution_sends_exactly_one_mock_send`.
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
     flow") — **1 parent**.
   - `d232e38` ("Merge Phase I mode-label fix: single authoritative
     harness status…") — **1 parent**, current `main` HEAD as of
     2026-11-08 (verified with `git cat-file -p d232e38`: 1 parent).
   - All three are **single-parent commits, not merges**, despite the word
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
  - **Code side of H6 — DONE 2026-11-08** (branch
    `topai-harness-hardening-staging`, commit `5eb2f04`): the staging
    safety guards in §2a (mock SMS provider, drafts_only lock,
    boot-time staging safety) are implemented and tested (21 tests,
    `tests/test_harness_staging_safety.py`). **Re-verified 2026-11-08**
    on branch `launch-staging-verify` (38 tests): 5 holes found and
    fixed — workers now run the boot guard (W1), Twilio/SimpleTexting
    credentials refused (W2), SendGrid/SMTP refused instead of warned
    (W3), Vapi/OpenAI keys refused (W4), and the approved mock send
    made reachable (W5). What remains is purely the Railway-dashboard
    creation below — no further code is needed to create staging.
  - **Railway status note (2026-11-08):** Railway's status page still
    shows the "External network blocks affecting access to Railway"
    incident as a partial outage (identified Nov 4; some users
    reporting restored access, access still affected from some
    networks), plus a separate ongoing "GitHub Outage Affecting Builds"
    incident from Nov 8, 10:11 UTC. If the dashboard or a staging
    deploy is unreachable or a build fails, retry from another network
    and re-check https://status.railway.com/ before assuming a repo or
    config problem.
  - **Railway dashboard state — NOT VERIFIED by this work.** No
    read-only dashboard look was possible from this subagent (no live
    browser access at this depth); the services/environments listed in
    §1 are the last recorded state (production `web`, `worker`,
    Postgres in project `gracious-expression`, per the acceptance
    report). Confirm the live service list when creating staging.
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

## 8. Staging design decision — same project, separate services

**Recommendation: create staging inside the existing Railway project
`gracious-expression`, as separate services (`staging`,
`staging-worker`) plus a separate Postgres — not a separate Railway
project, and not a Railway "environment".**

Reasons:
- One bill, one project to watch; the staging services are visibly
  distinct names next to production, so nobody deploys staging config
  into a production service by switching an environment tab.
- Same GitHub repo/branch flow as production (staging deploys the
  harness branch; production auto-deploys `main`), so the staging
  deploy exercises the real build path.
- Service-level variable isolation is total: staging services only
  ever see the variables set on them. A Railway environment would
  share service names across environments and rely on remembering
  which environment is active — a weaker, easier-to-confuse boundary
  for a two-person operation.
- Matches the spec already written in §1 and the step-by-step
  checklist in `docs/harness-railway-checklist.md` step A.

Topology mirrors production exactly: web (gunicorn, migrations first)
+ worker + Postgres. The production worker runs
`workers.sms_campaign_worker`; the staging worker runs
`workers.harness_worker` (see §1 — the production harness-worker
service's existence is itself an open dashboard check, R7). Staging
does **not** run `sms_campaign_worker`: with the mock provider on it
could not send, but there is no reason to run campaign sends from
staging at all.

## 9. Appendix — full environment-variable inventory

Every variable the app reads (`config.py`, `harness/alerts.py`,
`harness/costs.py`), with the staging value and whether it **must
differ** from production. "Leave unset" means: do not set the variable
on the staging services at all. Secrets are never pasted into this
repo or into chat — set them in the Railway dashboard only.

| Variable | Production (reference) | Staging value | Must differ? |
|---|---|---|---|
| `APP_ENV` / `ENV` | `production` | `staging` | Yes |
| `DATABASE_URL` | Production Postgres | Staging Postgres (separate service) | Yes — never the production URL |
| `DATABASE_PATH` | Unused (Postgres) | Unused | No |
| `FLASK_SECRET_KEY` | Production secret | Fresh random secret | Yes |
| `ANTHROPIC_API_KEY` | Production key | Real key (same account OK; metered, cost-tracked in `harness_costs`) | No (same account acceptable) |
| `CLAUDE_MODEL` / `ASK_TOPAI_MODEL` / `SCRIPT_CLAUDE_MODEL` | Defaults (`claude-opus-4-6` / `claude-sonnet-5` / `claude-sonnet-5`) | Defaults | No |
| `OPENAI_API_KEY` | Set (realtime voice) | Leave unset — **refused at boot** (hole W4, §2a G3) | Yes — unset |
| `ASK_TOPAI_REALTIME_MODEL` / `ASK_TOPAI_REALTIME_VOICE` | Defaults | Defaults | No |
| `PORT` | Set by Railway | Set by Railway | No |
| `APP_URL` / `APP_BASE_URL` | Production URL | Staging service URL | Yes |
| `SUBSCRIPTION_PRICE` | `$49/month` | Default | No |
| `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_ID` / `STRIPE_SUBSCRIPTION_PAYMENT_METHOD_CONFIGURATION` | Live keys | Leave unset (a live key is refused at boot, §2a G3) | Yes — unset |
| `SUBSCRIPTION_REQUIRED` | `true` | Leave default (`true`); tenant created directly in DB (§4) | No |
| `SENDGRID_API_KEY` | Set | Leave unset — **refused at boot** (hole W3, §2a G3) | Yes — unset |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` / `SMTP_USE_TLS` | SMTP fallback | Leave unset — `SMTP_HOST` **refused at boot** (hole W3) | Yes — unset |
| `FREE_ACCESS_EMAILS` | — | Leave unset | — |
| `REGISTRATION_ENABLED` | `false` | `false` | No |
| `REGISTRATION_ALLOWLIST` | — | Leave unset | — |
| `VOICE_PROVIDER` / `VOICE_PROVIDER_API_KEY` / `VOICE_PROVIDER_WEBHOOK_SECRET` | `vapi` + key | Leave Vapi keys unset — key **refused at boot** (hole W4, §2a G3) | Yes — unset |
| `REAL_ESTATE_LEAD_QUALIFIER_ASSISTANT_ID` / `VOICE_DEFAULT_ASSISTANT_ID` / `VOICE_PHONE_NUMBER_ID` / `VOICE_CALL_FROM_NUMBER` / `VOICE_DAILY_CALL_LIMIT` | Production Vapi config | Leave unset / defaults | Yes — unset |
| `SMS_PROVIDER` | `telnyx` | `telnyx` | No |
| `TWILIO_ACCOUNT_SID` / `TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER` / `TWILIO_MESSAGING_SERVICE_SID` | Diagnostic keys (if any) | Leave unset — real credentials **refused at boot** when `SMS_PROVIDER=twilio` and mock off (hole W2) | Yes — unset |
| `SIMPLETEXTING_API_TOKEN` / `SIMPLETEXTING_WEBHOOK_SECRET` / `SIMPLETEXTING_PHONE_NUMBER` / `SIMPLETEXTING_API_BASE` | — | Leave unset — token **refused at boot** when `SMS_PROVIDER=simpletexting` and mock off (hole W2) | Yes — unset |
| `TELNYX_API_KEY` | Production key | Leave unset (mock needs no key); if set, never the production key | Yes — do not copy |
| `TELNYX_MESSAGING_PROFILE_ID` | Production profile | Leave unset (or a staging-only profile) | Yes — unset |
| `TELNYX_PHONE_NUMBER` | `+18888210810` | Leave unset (mock needs no sender); the production number is refused at boot (§2a G3) | Yes — unset |
| `TELNYX_PUBLIC_KEY` | Production key | Leave unset | Yes — unset |
| `SMS_SUPPORT_DISPLAY` / `SMS_SUPPORT_E164` | `(888) 821-0810` / `+18888210810` | Defaults (display copy only; not a sender) | No |
| `TELNYX_TRIAL_MODE` | `true` | `true` | No |
| `TELNYX_VERIFIED_TEST_NUMBER` | — | One tester-owned mobile, E.164 | Yes — staging-only value |
| `TELNYX_API_BASE` / `TELNYX_WEBHOOK_TOLERANCE_SECONDS` | Defaults | Defaults | No |
| `TELNYX_TOLL_FREE_VERIFICATION_STATUS` | `verified` | Leave unset (not `verified`) | Yes |
| `SMS_AI_AUTO_REPLY_ENABLED` | `true` (default) | Leave unset (default `true`) — see §2, hole W5 | No (default) |
| `SMS_AI_MAX_REPLIES_PER_CONTACT_PER_DAY` / `SMS_DAILY_LIMIT` / `SMS_CAMPAIGN_MAX_RECIPIENTS` / `SMS_IMPORT_MAX_ROWS` / `SMS_MSGS_PER_MINUTE` / `SMS_MSGS_PER_HOUR` / `SMS_MSGS_PER_DAY` / `SMS_MAX_PER_CONTACT_PER_DAY` / `SMS_MAX_RETRIES` / `SMS_QUIET_HOURS_START` / `SMS_QUIET_HOURS_END` | Defaults | Defaults | No |
| `SMS_PROVIDER_MSGS_PER_SECOND` / `SMS_CAMPAIGN_WORKER_AVAILABLE` | Defaults | Defaults | No |
| `CONSENT_UPLOAD_DIR` / `CONSENT_UPLOAD_MAX_BYTES` / `SMS_IMPORT_UPLOAD_DIR` / `SMS_IMPORT_MAX_BYTES` | Defaults | Defaults | No |
| `SMS_TERMS_VERSION` / `SMS_CERT_TEXT_VERSION_*` | Defaults | Defaults | No |
| `LISTING_GENERATION_RETENTION_DAYS` | Default (60) | Default | No |
| `SOCIAL_TOKEN_ENCRYPTION_KEY` / `INTEGRATION_CREDENTIAL_ENCRYPTION_KEY` | Production keys | Fresh staging keys (never copy production's) | Yes |
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` / `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` / `META_APP_REVIEW_APPROVED` / `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` / `TIKTOK_AUDIT_APPROVED` | Production creds | Leave unset / `false` | Yes — unset |
| `ALLOW_DESTRUCTIVE_DB_RESET` / `ALLOW_SQLITE_TABLE_REBUILD` / `RUN_DEMO_SEED_ON_STARTUP` | `false` | Unset / `false` (refused at boot if `true`) | No (already false) |
| `HARNESS_MOCK_PROVIDERS` | Unset (off) | `true` (also the staging default — see §2a G1) | Yes |
| `HARNESS_MONTHLY_SPEND_CAP_CENTS` | `5000` ($50) | `500` ($5) | Yes — lower |
| `HARNESS_ANTHROPIC_PRICES_JSON` / `HARNESS_ANTHROPIC_PRICE_<MODEL>_INPUT/OUTPUT_CENTS_PER_M` | — | Same as production if set | No |
| `HARNESS_ALERT_WEBHOOK_URL` | Production webhook (unset today — R8) | Staging test webhook (different channel/endpoint) | Yes — separate |
| `HARNESS_ALERTS_ENABLED` | Default (enabled) | Default | No |
| `HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD` | Default (3) | Default | No |

## 10. Low-cost staging definition + monthly cost estimate (2026-11-08)

Complete, minimal definition for the staging environment Ren creates
in the Railway dashboard. Nothing here is deployed yet — **BLOCKED**
on the dashboard steps in §10.3. The step-by-step dashboard
walkthrough with navigation paths is
`docs/harness-railway-checklist.md` step A; this section is the
definition that walkthrough implements (and corrects its
`SMS_AI_AUTO_REPLY_ENABLED=false` row — see §2, hole W5).

### 10.1 Topology (minimal)

Same Railway project **`gracious-expression`**, same workspace — no
new project, no new plan, no Railway "environment". Three services,
nothing else:

| Service | Type | Start command | Replicas | Notes |
|---|---|---|---|---|
| `staging` | Web (Nixpacks, same repo) | `python -m migrations.runner && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` | 1 | Same as production `web`. Deploys the harness branch under test (see §10.3 step 3); never `main` unless Ren explicitly directs it. |
| `staging-worker` | Worker | `python -m workers.harness_worker` | 1 | Harness job loop only. Do NOT run `workers.sms_campaign_worker` in staging. |
| Staging Postgres | Database (Railway Postgres template) | — (Railway-managed) | 1 | Separate database, separate `DATABASE_URL`. No backups/HA — staging data is disposable demo data. |

No Redis, no extra workers, no paid add-ons, no custom domain
(the generated `*.up.railway.app` domain is enough), no volume
beyond what the Postgres template provisions (1 GB is plenty for
demo data).

### 10.2 Environment variables (complete set for both services)

Set on **both** `staging` and `staging-worker`. Values that **must
differ from production** are marked ★. Anything not listed here is
left **unset** on staging — in particular, never copy production's
Vapi keys, SendGrid key, SMTP config, Telnyx API key / messaging
profile, Stripe keys, or social OAuth secrets into staging. The boot
guard (§2a G3) refuses the dangerous ones, but the rule is: if it is
not in this table, do not set it.

| Variable | Staging value | ★ Must differ? |
|---|---|---|
| `DATABASE_URL` | Staging Postgres URL (from the staging Postgres service Variables) | ★ Yes — never production's |
| `APP_ENV` | `staging` | ★ Yes |
| `FLASK_SECRET_KEY` | Fresh random secret (generate one; do not reuse production's) | ★ Yes |
| `ANTHROPIC_API_KEY` | Real key (same account is fine; metered, cost-tracked in `harness_costs`) | No |
| `SMS_PROVIDER` | `telnyx` | No |
| `TELNYX_TRIAL_MODE` | `true` | No |
| `TELNYX_VERIFIED_TEST_NUMBER` | One tester-owned mobile, E.164 (e.g. Ren's) | ★ Yes — staging-only value |
| `HARNESS_MOCK_PROVIDERS` | `true` (also the staging default — belt-and-braces) | ★ Yes |
| `HARNESS_MONTHLY_SPEND_CAP_CENTS` | `500` ($5) | ★ Yes — lower |
| `HARNESS_ALERT_WEBHOOK_URL` | Staging test webhook (different channel/endpoint) or leave unset for the first run | ★ Yes — separate |
| `REGISTRATION_ENABLED` | `false` | No |
| `SMS_AI_AUTO_REPLY_ENABLED` | Leave unset (default `true`) — see §2, hole W5 | No |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` / `STRIPE_PUBLISHABLE_KEY` / `STRIPE_WEBHOOK_SECRET` | Leave unset (live key refused at boot) | ★ Yes — unset |
| `SENDGRID_API_KEY` / `SMTP_*` | Leave unset (refused at boot) | ★ Yes — unset |
| `VOICE_PROVIDER_API_KEY` / `OPENAI_API_KEY` | Leave unset (refused at boot) | ★ Yes — unset |
| `TELNYX_API_KEY` / `TELNYX_PHONE_NUMBER` / `TELNYX_MESSAGING_PROFILE_ID` / `TELNYX_PUBLIC_KEY` | Leave unset (mock needs none; production number refused at boot) | ★ Yes — unset |
| `TWILIO_*` / `SIMPLETEXTING_*` | Leave unset (refused at boot when selected + mock off) | ★ Yes — unset |
| `TELNYX_TOLL_FREE_VERIFICATION_STATUS` | Leave unset (not `verified`) | ★ Yes |
| `SOCIAL_TOKEN_ENCRYPTION_KEY` / `INTEGRATION_CREDENTIAL_ENCRYPTION_KEY` | Fresh staging keys if ever needed; leave unset for the first run | ★ Yes — never copy production's |
| `ALLOW_DESTRUCTIVE_DB_RESET` / `ALLOW_SQLITE_TABLE_REBUILD` / `RUN_DEMO_SEED_ON_STARTUP` | Unset / `false` | No (already false) |

Full inventory with every variable the app reads: §9.

### 10.3 Monthly cost estimate — ~$15/month (verified Railway rates)

Rates verified 2026-11-09 against Railway's official pricing page
(https://railway.com/pricing, crawled 2026-11-09): usage is billed
per second at Memory **$0.00000386/GB/sec** (≈ **$10.00/GB-month**),
CPU **$0.00000772/vCPU/sec** (≈ **$20.01/vCPU-month**), Volumes
**$0.00000006/GB/sec** (≈ **$0.16/GB-month**), Egress **$0.05/GB**.
The **Hobby plan is $5/month and includes $5 of usage** — the $5
included-usage rule is Railway's own pricing FAQ on that page ("If
your total usage exceeds $5 in any given billing period, you will be
charged total usage" — i.e. the bill is the greater of the $5 plan
fee and metered usage), and the $5/month fee is corroborated by
multiple independent pricing write-ups that re-checked
railway.com/pricing in Oct–Nov 2026. These are the vendor's
published list prices; re-check them at railway.com/pricing before
relying on the total, because Railway reprices periodically.

Assumptions (minimal, near-idle staging used for demos only — the
services run 24/7 because Railway bills what the container holds,
idle or not; there is no scale-to-zero on Hobby):

| Service | Assumed avg usage | Monthly cost |
|---|---|---|
| `staging` web (gunicorn, 2 workers) | 0.10 vCPU, 0.25 GB RAM | CPU $2.00 + RAM $2.50 = **$4.50** |
| `staging-worker` (harness worker, mostly idle polling) | 0.05 vCPU, 0.25 GB RAM | CPU $1.00 + RAM $2.50 = **$3.50** |
| Staging Postgres | 0.10 vCPU, 0.50 GB RAM, 1 GB volume | CPU $2.00 + RAM $5.00 + volume $0.16 = **$7.16** |
| Egress (demo traffic only) | ~1 GB | **$0.05** |
| **Total metered usage** | | **≈ $15.21/month** |

**Bottom line: ≈ $15/month.** Because staging lives in the same
project/workspace as production, there is **no second plan fee** —
the $5 Hobby fee is already paid once for the workspace, and staging
adds its metered usage on top of the current bill (incremental
≈ $15/month). If staging were the only workload in a workspace, the
bill would be the same ≈ $15 (usage above the $5 credit). Planning
range: **$10–$20/month** depending on observed CPU/RAM (check the
first invoice's per-service usage and adjust the assumptions here).
Floor: $5/month if usage ever fell under the $5 credit (it will not
with three always-on services). No paid add-ons are required.

Non-Railway costs in staging: **$0** for Telnyx (mock provider —
no sends, no segments), **$0** for Stripe (unset), **$0** for
SendGrid/Vapi/OpenAI (unset, boot-refused). Anthropic is the only
real metered spend: the demo's handful of Sonnet draft calls cost
cents, and harness spend is capped at **$5/tenant/month** in
staging — but see the residual risk in §2a: Ask TopAI and listing
generation are NOT cap-enforced, so keep staging demo traffic to
the harness paths or watch Anthropic usage.

### 10.4 What Ren must do (BLOCKED — nothing below has been done)

1. Open railway.com, log in with the account that owns
   `gracious-expression`, open the project.
2. **Staging Postgres first:** project canvas → New → Database →
   PostgreSQL. Wait for healthy/running. Record its service name and
   confirm its `DATABASE_URL` differs from production's.
3. **Staging web:** New → GitHub Repo → same repo as production →
   branch under test (the harness branch, currently
   `launch-staging-verify` once merged/approved for staging — use
   `main` only if Ren explicitly directs it). Name it `staging`.
   Set the start command from §10.1 (Settings → Start Command).
   Leave build defaults matching production (Nixpacks).
4. **Staging worker:** same way, name it `staging-worker`, start
   command `python -m workers.harness_worker`.
5. **Variables:** on both services, set exactly the §10.2 table.
   Generate a fresh `FLASK_SECRET_KEY` (do not reuse production's).
   Use Ren's mobile as `TELNYX_VERIFIED_TEST_NUMBER`. Do not paste
   any production secret.
6. **Deploy** both services (they should deploy on creation;
   otherwise Deployments → Deploy/Redeploy). Expected: successful
   deployments; `staging` logs show `engine=postgres`,
   `postgres_active=true`, migrations up to 032, no secret values.
   If a service crashes on boot, read its Deploy Logs — the usual
   cause is a missing/wrong variable, and since the re-verification
   the log will name the refused variable with a `FATAL:` line.
7. **Verify:** open the staging URL — the login page loads. Create
   the demo tenant directly in the staging DB (signup returns 503
   without Stripe — see §4), log in, confirm its
   `harness_settings` row is `mode='drafts_only', paused=0`, and
   confirm `GET /api/harness/status` reports `drafts_only`,
   `0 pending approvals/jobs`.
8. **Run the §4 demo** (inbound → draft → approval → exactly one
   mock send) and the §5 rollback demo in staging; record dates and
   results in `docs/harness-railway-checklist.md` §7. Do not mark
   H6 closed until both pass.

Detailed navigation paths, expected outcomes per step, and the
completion log live in `docs/harness-railway-checklist.md` step A
and §7 — follow them alongside this section. **One correction to
that checklist:** its step A still lists
`SMS_AI_AUTO_REPLY_ENABLED=false`; the correct staging value is
**leave unset (default `true`)** per §2 (hole W5) — the checklist
row has been corrected to match.
