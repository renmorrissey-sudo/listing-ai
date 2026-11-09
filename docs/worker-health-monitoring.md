# Worker health monitoring — harness worker liveness, stuck jobs, alerts

The harness worker is a separate Railway service (`harness-worker`
Procfile entry, `python -m workers.harness_worker`). It writes a durable
heartbeat row (`sms_worker_heartbeats`, worker_id
`harness-<hostname>-<pid>`) at boot, every loop iteration, and every 10 s
from a background thread while a long dispatch runs. A dead worker cannot
alert its own death — so the watching is done by the **web process**,
which is alive precisely when the worker may not be.

## What is checked, and where

All logic lives in `harness/worker_health.py`; the status API, the
`/harness` page, and `GET /api/harness/health` all read from it, so the
three surfaces can never disagree.

| Signal | Definition | Threshold |
|---|---|---|
| Worker stale | Freshest `harness-%` heartbeat row is older than the window, or no row exists at all (service never created / never heartbeated) | 120 s (`WORKER_STALE_AFTER_SECONDS`) — the same window the status API and the reclaim guard use. A live worker heartbeats every 1–10 s, so 120 s of silence means down, crashed, or never deployed. |
| Stuck job | Job left `claimed`/`running` past the threshold **and** its claiming worker has no fresh `running` heartbeat (the live-worker guard from `reclaim_stale_jobs`) | 15 min (`STUCK_JOB_AFTER_SECONDS`) — 3× the 300 s reclaim timeout. A long job on a live worker is *not* stuck (fresh heartbeat excludes it); a job whose owner is gone and which no live process will recover *is* stuck. |
| Overdue job | `pending` job whose `next_attempt_at` (scheduled retry) — or `created_at` when no backoff is set — is past the threshold | 15 min. A healthy worker claims due jobs within seconds. |

`check_worker_health(user_id)` returns `{"worker": {...}, "stuck_jobs",
"stuck_job_ids" (first 10), "overdue_jobs", "healthy"}`. `healthy` is
True only when the heartbeat is fresh AND there are no stuck or overdue
jobs. `user_id` scopes the stuck/overdue counts to one tenant (the
status API / page); `None` counts platform-wide (the health endpoint
under token auth).

## Alerting (web side)

`maybe_alert_worker_health(health, user_id)` sends through
`harness.alerts.notify_event` — webhook POST when
`HARNESS_ALERT_WEBHOOK_URL` is configured, else an audit row + log line
(`action="ops_alert"`). Events:

- `worker_stale` — heartbeat stale or never seen. Detail: worker id,
  last seen, age in seconds, `never_seen` flag, stuck/overdue counts.
  No customer data, no secrets (same redaction as all ops alerts).
- `stuck_jobs` — one or more stuck jobs. Detail: count + job ids.
- `worker_recovered` — the heartbeat is fresh again after a recorded
  stale episode. Closes the episode so a later outage alerts again.

Dedup: hour-scoped keys (`worker_stale|hour=...`), in memory and
durable via the `ops_alert` audit row — a dead worker re-alerts hourly,
not on every check, and a restarted web process does not re-send an
alert it already sent. Recovery detection reads the latest
`worker_stale` / `worker_recovered` audit event, so it survives restarts
too.

The check runs on every `GET /api/harness/health` call and on every
`/harness` page load. `get_harness_status()` itself stays side-effect
free — it reports health, never alerts.

## External watcher (when the whole deployment is down)

The web-side check cannot fire when the web process itself is down.
That case is covered by an external monitor, not by another paid
service:

1. Set a token: `HARNESS_HEALTH_TOKEN=<long random string>` on the web
   service (Railway variables; same value for staging/production must
   differ). The token grants **read-only health reads only** — it
   authorizes nothing else.
2. Point a free uptime monitor at it (e.g. UptimeRobot free plan,
   5-minute interval):
   `GET https://topairealestatetools.com/api/harness/health?token=<token>`
   — or the `X-Harness-Health-Token` header. The endpoint returns 200
   with `{"healthy": true, ...}` when the worker is fresh and no jobs
   are stuck; `{"healthy": false, ...}` when the worker is stale or
   jobs are stuck; 503 when the check itself cannot run. The monitor
   alerts on non-200 **or** on `healthy: false` (UptimeRobot keyword
   monitoring; any monitor that can match response content works).
3. When the whole deployment is down the monitor alerts on HTTP
   failure (connection error / 5xx) — that is exactly the signal a
   dead deployment gives, and no in-app code can produce it.

Without the token the endpoint requires login, so the monitor path
only exists when the token is set. Rotate the token like any credential
if it ever leaks; it appears nowhere in logs (the token value is never
logged — only a failed-auth 401).

## What to do on each alert

- `worker_stale` (never seen): the `harness-worker` service does not
  exist or never started — create/verify it in Railway (see
  `docs/harness-worker-architecture.md` §6) and confirm
  `worker.running=true` in the status API.
- `worker_stale` (last seen N s/min ago): the service crashed or was
  stopped — check Railway service logs + the `stopped` heartbeat row;
  restart the service. Jobs it had claimed are reclaimed by
  `reclaim_stale_jobs` once the worker restarts (or by a new worker).
- `stuck_jobs`: jobs are stranded on a dead worker. Restart the worker
  (reclaim recovers them) and check why the worker died — the
  `stuck_job_ids` in the alert detail identify the jobs; the audit log
  (`harness_audit`) shows their history.
- `worker_recovered`: informational — no action; the episode is closed.

## Thresholds

Tune in `harness/worker_health.py`: `WORKER_STALE_AFTER_SECONDS`
(120 — must stay in sync with the status API window and
`harness.jobs.HEARTBEAT_FRESH_SECONDS`) and `STUCK_JOB_AFTER_SECONDS`
(900). Shortening the stuck threshold below ~600 s risks flagging jobs
reclaim is still allowed to recover; lengthening it delays the signal.
