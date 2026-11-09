# Harness failure-alerting — production webhook setup

Status: **BLOCKED — live delivery not yet proven.** No production webhook
exists and no real alert has ever been sent to Slack/Discord. Isolated
E2E delivery is proven (local listener only); see
`docs/alerting-e2e-evidence-2026-11-08.json` and
`scripts/verify_alerting_e2e.py`. Do not claim production alerting works
until the verification in §5 below passes against the real webhook.

## 1. What the webhook receives

`harness/alerts.py` POSTs one JSON object per alert (stdlib `urllib`,
5 s timeout, non-2xx = delivery failure) to `HARNESS_ALERT_WEBHOOK_URL`:

```json
{
  "event": "dead_letter",
  "key": "dead_letter:123",
  "title": "Harness job 123 dead-lettered (sms_auto_reply)",
  "detail": {"job_id": 123, "user_id": 7, "correlation_id": "<uuid4 hex>",
             "kind": "sms_auto_reply", "attempts": 5,
             "error": "[REDACTED]", "payload_keys": ["lead_id"]},
  "timestamp": "2026-11-08T20:00:00+00:00",
  "text": "[TopAI Harness] Harness job 123 dead-lettered (sms_auto_reply) (event=dead_letter key=dead_letter:123)",
  "content": "[TopAI Harness] Harness job 123 dead-lettered (sms_auto_reply) (event=dead_letter key=dead_letter:123)"
}
```

Events: `dead_letter`, `provider_failure` (after 3 consecutive retryable
failures for one provider, default threshold, re-alerts hourly while still
broken), `worker_failure` (dispatch/loop crash, hourly per error
signature), `spend_alert` (warning at 80% of the $50/tenant/month cap,
limit at 100%, once per tenant/level/month). Every payload carries the
same redacted one-liner under both `text` (Slack) and `content`
(Discord); payload values never travel — only key names — and secrets,
phone numbers, emails, and street addresses are redacted before send.
`correlation_id` passes through verbatim by design (it is a trace id,
already stored in plaintext in the audit log).

With no webhook URL set, alerting is audit + log only (disabled by
default). `HARNESS_ALERTS_ENABLED=false` force-disables sending even
with a URL set.

> Note: `docs/harness-railway-checklist.md` § "Alerting path" still says
> spend alerts are "deliberately not external". That is stale: on this
> branch `record_spend_alert` fans out to the webhook when one is
> configured, proven by the E2E run (2 `spend_alert` POSTs, warning +
> limit). The checklist line needs updating, not the code.

## 2. Create the webhook (Ren — pick one)

### Slack (recommended)

1. Slack workspace → the channel for TopAI ops alerts (create one, e.g.
   `#topai-ops`, so alerts don't land in a general channel).
2. Channel settings → Integrations → Add apps → **Incoming Webhooks**
   → Add to Slack → pick the channel → copy the URL
   (`https://hooks.slack.com/services/…`).
3. Keep the URL secret — anyone with it can post to the channel. Do not
   paste it into chat, docs, or the repo; it goes only into the Railway
   variable in §3.

### Discord (alternative)

1. Server settings → Integrations → Webhooks → New Webhook → pick the
   channel → copy the webhook URL (`https://discord.com/api/webhooks/…`).
2. Same secrecy rule as Slack. Discord accepts the same payload via the
   `content` field.

Use **separate webhooks for production and staging** (different channels,
or one channel with the staging webhook used only in tests). Never point
staging at the production webhook: staging tests deliberately dead-letter
jobs and fire provider-failure alerts.

## 3. Set the variable in Railway (Ren, or whoever holds Railway access)

Project: `gracious-expression` (per `docs/harness-railway-checklist.md`).

| Variable | Value | Services |
|---|---|---|
| `HARNESS_ALERT_WEBHOOK_URL` | the webhook URL from §2 | **both** `web` **and** `harness-worker` |
| `HARNESS_ALERTS_ENABLED` | leave **unset** (enabled by default) | — |
| `HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD` | leave unset (default 3) unless Ren wants a different threshold | both, if set |

Why both services: the worker fires `dead_letter` / `provider_failure` /
`worker_failure` alerts; the web process fires `spend_alert` via the
policy gate (`harness/policy.py` → `record_spend_alert`). Setting the
variable on only one service leaves the other audit-only. In Railway,
set it as a shared/project variable if the project supports it, otherwise
set the identical value on each service. Railway redeploys on variable
change (expected behaviour — not verified on this project; redeploy the
affected services manually if they don't restart).

## 4. Verify the webhook endpoint itself (before touching the app)

From any shell (no app needed). Replace the URL with the real one:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"text": "[TopAI Harness] webhook smoke test — please delete this message", "content": "[TopAI Harness] webhook smoke test — please delete this message"}' \
  'https://hooks.slack.com/services/REPLACE/ME'
```

Expected: HTTP `200` and the test message appears in the channel. Any
non-2xx means the URL is wrong or the webhook was deleted — fix that
before §5. Delete the test message afterwards.

## 5. Verify a live alert end-to-end (staging first, then production)

**Staging first.** Set the staging webhook URL on `staging` /
`staging-worker`, then force a dead-letter in staging (enqueue a job
with an unknown kind, or stop the staging worker mid-job repeatedly
until a job dead-letters — see `docs/harness-staging.md` step G).
Expected: exactly one alert POST in the staging channel, and one
`ops_alert` audit row (§6).

**Production.** Do not force failures in production. The first real
dead-letter / provider-failure / spend-limit event is the live test.
Confirm it landed with the audit query in §6 and the message in the
channel. Until such an event occurs, production delivery is
**unproven but configured** — say exactly that, not "working".

## 6. Audit queries — confirm a live alert landed

Postgres (production, via `psql` / Railway Postgres query tab):

```sql
-- alerts actually sent (one row per deduped event)
SELECT id, user_id, gate_reason, correlation_id, created_at
FROM harness_audit
WHERE action = 'ops_alert'
ORDER BY created_at DESC LIMIT 10;

-- failed deliveries (webhook down; each is retried on next re-trigger)
SELECT id, user_id, gate_reason, correlation_id, created_at
FROM harness_audit
WHERE action = 'ops_alert_failed'
ORDER BY created_at DESC LIMIT 10;

-- spend alerts (audit-only until a webhook is set, then also POSTed)
SELECT id, user_id, gate_reason, cost_cents, created_at
FROM harness_audit
WHERE action = 'spend_alert'
ORDER BY created_at DESC LIMIT 10;
```

SQLite (local dev / tests) — same queries, `?` placeholders not needed
for these read-only selects.

A sent alert has an `ops_alert` row whose `gate_reason` contains
`key=<event>:<id>`; a failed delivery has **no** `ops_alert` row for that
key and **one** `ops_alert_failed` row per failed attempt. If the channel
got the message but there is no `ops_alert` row, the alert did not come
from this code path — investigate before trusting it.

## 7. What Ren needs to do (exact action)

1. Create the Slack incoming webhook (§2, Slack) — or Discord — in a
   dedicated `#topai-ops` channel, production and staging separate.
2. In Railway project `gracious-expression`, set
   `HARNESS_ALERT_WEBHOOK_URL` to the production URL on **both** the
   `web` and `harness-worker` services (shared variable if supported).
3. Run the §4 curl smoke test, then the §5 staging dead-letter test.
4. Tell Muse the staging test passed (or paste the channel message);
   only then is production alerting "configured, awaiting first live
   event" — it is **not** "verified live" until a real alert lands
   and §6 confirms it.

## 8. Evidence on this branch

- `scripts/verify_alerting_e2e.py` — independent re-verification script
  (real local listener, 29 checks, all passed 2026-11-08).
- `docs/alerting-e2e-evidence-2026-11-08.json` — captured payloads and
  per-scenario results: 11 POSTs (10× HTTP 200, 1× deliberate HTTP 500),
  events dead_letter 7 / provider_failure 1 / worker_failure 1 /
  spend_alert 2; zero leaks in field-by-field inspection.
- Tests: `tests/test_harness_alerts.py` (15) + `tests/test_harness_alerts_e2e.py`
  (16) = **31 passed**; adjacent `test_harness_worker.py` +
  `test_harness_policy_gate.py` + `test_harness_costs.py` = 68 passed.
  (The brief's "76 alerting tests" does not match this branch's files:
  the two alerting files hold 31 tests; no other file is alerting-specific.)

## 9. Known gaps (not covered by this alerting — see final report)

- Stuck-claimed jobs / reclaim storms: no alert (only a log line).
- Heartbeat loss (worker process dead): no alert; only passive status
  on `/api/harness/status`.
- Provider-failure counter is in-memory: a restart resets it, so a
  still-broken provider needs 3 fresh failures before re-alerting.
