"""Worker health monitoring for the AI Harness (Phase 1 hardening).

Problem: a dead worker cannot alert its own death. The harness worker
writes a durable heartbeat row (``sms_worker_heartbeats``, worker_id
``harness-<hostname>-<pid>``) at boot, every loop iteration, and every
10 s from a background thread while a long dispatch runs — but nothing
watched that row from the outside. The status API reported
``worker.running`` (fresh heartbeat within 120 s) and nothing else: no
stuck-job signal, no alert when the worker went silent.

This module is the WEB-side watcher. It runs in the web process (which
is alive precisely when the worker may not be) and answers three
questions from durable DB state only:

1. Is the harness worker alive? ``get_worker_heartbeat`` reads the
   freshest ``harness-%`` heartbeat row. ``stale`` is True when no row
   exists (the service was never created / never heartbeated) or the
   freshest row is older than ``WORKER_STALE_AFTER_SECONDS`` (120 s,
   the same window the status API and the reclaim guard use). A live
   worker heartbeats every 1-10 s, so 120 s of silence means down,
   crashed, or never deployed — never a healthy worker.
2. Are jobs stuck? ``count_stuck_jobs`` counts jobs left
   ``claimed``/``running`` past ``STUCK_JOB_AFTER_SECONDS`` (15 min)
   whose claiming worker has NO fresh ``running`` heartbeat — the same
   live-worker guard ``reclaim_stale_jobs`` uses. A long job on a live
   worker is NOT stuck (its heartbeat is fresh, so the guard excludes
   it); a job whose owner is gone and which no live process will
   recover IS stuck. ``count_overdue_jobs`` counts ``pending`` jobs
   whose ``next_attempt_at`` (or ``created_at`` when no backoff is set)
   is past the same threshold — due work no worker picked up.
3. Should an operator be told? ``maybe_alert_worker_health`` sends
   ``worker_stale`` / ``stuck_jobs`` / ``worker_recovered`` events
   through ``harness.alerts.notify_event`` — i.e. the webhook notifier
   when configured, else an audit row + log line. Dedup is hour-scoped
   (a dead worker re-alerts hourly, not on every check) and durable
   (the ``ops_alert`` audit row is the backstop across restarts, so a
   restarted web process does not re-send an alert it already sent).
   Recovery is a first-class event: when the worker is fresh again
   after a recorded stale episode, one ``worker_recovered`` alert is
   sent and the episode is closed, so a later outage alerts again.

The check is triggered from the web side only: ``GET
/api/harness/health`` (see ``harness.routes``) runs it on every call,
and the ``/harness`` status page runs it on load. ``get_harness_status``
itself stays side-effect free — it only *reports* the health dict.

No new infrastructure: no new service, no paid monitor. For the case
where the whole deployment is down (web dead too), see
``docs/worker-health-monitoring.md`` — a free external uptime monitor
hitting ``/api/harness/health?token=...`` is the documented watcher;
it alerts on HTTP failure, which is exactly the signal a dead
deployment gives.

Thresholds (documented, tunable in this module):
  WORKER_STALE_AFTER_SECONDS = 120  — heartbeat freshness window.
  STUCK_JOB_AFTER_SECONDS    = 900  — 15 min: well past the 300 s
      reclaim timeout, so a job is only "stuck" when neither the
      worker finished it nor reclaim recovered it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

# Heartbeat freshness window. Must match the status API and the
# reclaim guard (harness.jobs.HEARTBEAT_FRESH_SECONDS) — a worker
# heartbeats every 1-10 s, so 120 s of silence means down.
WORKER_STALE_AFTER_SECONDS = 120

# A claimed/running job is "stuck" only past this age AND with no live
# owner (see count_stuck_jobs). 15 min is 3x the 300 s reclaim timeout:
# a live worker's long job is protected by its fresh heartbeat, and a
# dead worker's jobs are reclaimable after 300 s — so past 15 min the
# job is stranded by something reclaim cannot fix (worker down and not
# restarted, or reclaim itself failing).
STUCK_JOB_AFTER_SECONDS = 900

# Cap on the stuck-job id list carried in the health dict / alert detail.
STUCK_JOB_ID_LIMIT = 10


def _parse_ts(value):
    """Parse an ISO timestamp string to an aware datetime, or None."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now():
    return datetime.now(timezone.utc)


def get_worker_heartbeat():
    """Freshest harness-worker heartbeat row, with staleness computed.

    Never raises. Returns ``{"running", "stale", "never_seen",
    "age_seconds", "last_seen_at", "worker_id"}``. ``running`` keeps
    the exact semantics of ``harness.routes._harness_worker_heartbeat``
    (status ``running`` and age within the freshness window); ``stale``
    is its complement when a row exists, and True when no row exists
    at all. ``age_seconds`` is None when the row is missing or its
    timestamp is unparseable.
    """
    try:
        with db.get_db() as conn:
            row = conn.execute(
                """
                SELECT worker_id, status, last_seen_at
                  FROM sms_worker_heartbeats
                 WHERE worker_id LIKE 'harness-%'
                 ORDER BY last_seen_at DESC, worker_id DESC
                 LIMIT 1
                """
            ).fetchone()
    except Exception:
        return {
            "running": False,
            "stale": True,
            "never_seen": True,
            "age_seconds": None,
            "last_seen_at": None,
            "worker_id": None,
        }
    if not row:
        return {
            "running": False,
            "stale": True,
            "never_seen": True,
            "age_seconds": None,
            "last_seen_at": None,
            "worker_id": None,
        }
    seen = _parse_ts(row["last_seen_at"])
    age = None
    if seen is not None:
        age = (_now() - seen).total_seconds()
    running = bool(
        row["status"] == "running" and age is not None and 0 <= age <= WORKER_STALE_AFTER_SECONDS
    )
    return {
        "running": running,
        "stale": not running,
        "never_seen": False,
        "age_seconds": age,
        "last_seen_at": row["last_seen_at"],
        "worker_id": row["worker_id"],
    }


def count_stuck_jobs(user_id=None, *, stuck_after_seconds=STUCK_JOB_AFTER_SECONDS):
    """(count, [job ids]) of jobs stranded on a dead worker.

    A job is stuck when it has sat ``claimed``/``running`` longer than
    ``stuck_after_seconds`` AND its claiming worker has no fresh
    ``running`` heartbeat — the live-worker guard from
    ``reclaim_stale_jobs``, so a long job on a live worker is never
    counted. ``user_id`` scopes the count to one tenant (the status
    API); None counts platform-wide (the health endpoint). Returns
    ``(0, [])`` — never raises — when the tables are missing.
    """
    cutoff = (_now() - timedelta(seconds=stuck_after_seconds)).isoformat()
    fresh_cutoff = (_now() - timedelta(seconds=WORKER_STALE_AFTER_SECONDS)).isoformat()
    scope_sql = " AND user_id = ?" if user_id is not None else ""
    scope_params = (user_id,) if user_id is not None else ()
    guarded_sql = f"""
        SELECT id FROM harness_jobs
        WHERE status IN ('claimed', 'running')
          AND claimed_at IS NOT NULL
          AND claimed_at <= ?
          {scope_sql}
          AND NOT EXISTS (
              SELECT 1 FROM sms_worker_heartbeats h
              WHERE h.worker_id = harness_jobs.claimed_by
                AND h.status = 'running'
                AND h.last_seen_at >= ?
          )
        ORDER BY claimed_at ASC
        LIMIT ?
    """
    plain_sql = f"""
        SELECT id FROM harness_jobs
        WHERE status IN ('claimed', 'running')
          AND claimed_at IS NOT NULL
          AND claimed_at <= ?
          {scope_sql}
        ORDER BY claimed_at ASC
        LIMIT ?
    """
    try:
        with db.get_db() as conn:
            rows = conn.execute(
                guarded_sql, (cutoff, *scope_params, fresh_cutoff, STUCK_JOB_ID_LIMIT)
            ).fetchall()
    except Exception:
        # Heartbeat table missing (or any guard failure): fall back to
        # the unguarded count rather than reporting zero stuck jobs.
        try:
            with db.get_db() as conn:
                rows = conn.execute(
                    plain_sql, (cutoff, *scope_params, STUCK_JOB_ID_LIMIT)
                ).fetchall()
        except Exception:
            return 0, []
    ids = [int(dict(r)["id"]) for r in rows]
    return len(ids), ids


def count_overdue_jobs(user_id=None, *, overdue_after_seconds=STUCK_JOB_AFTER_SECONDS):
    """Pending jobs due long ago that no worker picked up.

    A job is overdue when it is ``pending`` and its ``next_attempt_at``
    (a scheduled retry/backoff) is past the threshold, or — when no
    backoff is set — its ``created_at`` is past it. A healthy worker
    claims due jobs within seconds, so any overdue job means the worker
    is not processing. Returns 0 — never raises — when tables are missing.
    """
    cutoff = (_now() - timedelta(seconds=overdue_after_seconds)).isoformat()
    scope_sql = " AND user_id = ?" if user_id is not None else ""
    scope_params = (user_id,) if user_id is not None else ()
    try:
        with db.get_db() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM harness_jobs
                WHERE status = 'pending'
                  AND (
                    (next_attempt_at IS NOT NULL AND next_attempt_at <= ?)
                    OR (next_attempt_at IS NULL AND created_at <= ?)
                  )
                  {scope_sql}
                """,
                (cutoff, cutoff, *scope_params),
            ).fetchone()
        return int(row["n"]) if row and row["n"] is not None else 0
    except Exception:
        return 0


def check_worker_health(user_id=None):
    """Full worker-health snapshot. Never raises.

    ``user_id`` scopes the stuck/overdue counts to one tenant (the
    status API); None counts platform-wide (the health endpoint).
    ``healthy`` is True only when the worker heartbeat is fresh AND
    there are no stuck or overdue jobs.
    """
    worker = get_worker_heartbeat()
    stuck_count, stuck_ids = count_stuck_jobs(user_id)
    overdue = count_overdue_jobs(user_id)
    return {
        "worker": worker,
        "stuck_jobs": stuck_count,
        "stuck_job_ids": stuck_ids,
        "overdue_jobs": overdue,
        "healthy": bool(worker["running"] and stuck_count == 0 and overdue == 0),
    }


def _current_hour():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _has_unrecovered_stale_alert():
    """True if the latest worker_stale/worker_recovered audit event is a
    stale one — i.e. a stale episode was alerted and no recovery has
    been recorded since. Never raises; False on any error."""
    try:
        with db.get_db() as conn:
            row = conn.execute(
                """
                SELECT gate_reason FROM harness_audit
                WHERE action = 'ops_alert'
                  AND (gate_reason LIKE '% event=worker_stale %'
                       OR gate_reason LIKE '% event=worker_recovered %')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception:
        return False
    if not row:
        return False
    return "event=worker_stale " in str(dict(row)["gate_reason"])


def maybe_alert_worker_health(health, user_id=None):
    """Alert (once per UTC hour per condition) on worker-health problems.

    Runs in the WEB process — alive precisely when the worker may not
    be — so a dead worker's silence still reaches an operator. Uses
    ``harness.alerts.notify_event``: webhook POST when a notifier is
    configured, else an audit row + log line; dedup is hour-scoped in
    memory and durable via the ``ops_alert`` audit row, so repeat checks
    within the hour do not re-send. Recovery is a first-class event:
    when the worker is fresh again after a recorded stale episode, one
    ``worker_recovered`` alert closes the episode. Never raises.
    Returns ``{"worker_stale": bool, "stuck_jobs": bool,
    "worker_recovered": bool}`` — True where an external notification
    was actually sent on this call.
    """
    result = {"worker_stale": False, "stuck_jobs": False, "worker_recovered": False}
    try:
        from harness import alerts
    except Exception:
        logger.exception("worker health alerting unavailable")
        return result
    worker = (health or {}).get("worker") or {}
    hour = _current_hour()
    scope = str(user_id) if user_id is not None else "global"
    try:
        if worker.get("stale"):
            age = worker.get("age_seconds")
            detail = {
                "worker_id": worker.get("worker_id"),
                "last_seen_at": worker.get("last_seen_at"),
                "age_seconds": age,
                "never_seen": bool(worker.get("never_seen")),
                "stuck_jobs": int((health or {}).get("stuck_jobs") or 0),
                "overdue_jobs": int((health or {}).get("overdue_jobs") or 0),
            }
            if worker.get("never_seen"):
                title = "Harness worker has never heartbeated — the harness-worker service may not exist"
            else:
                title = (
                    "Harness worker heartbeat stale"
                    + (f" (last seen {age:.0f}s ago)" if isinstance(age, (int, float)) else "")
                )
            result["worker_stale"] = bool(
                alerts.notify_event(
                    "worker_stale",
                    f"worker_stale|hour={hour}",
                    title,
                    detail=detail,
                    user_id=user_id,
                )
            )
        stuck = int((health or {}).get("stuck_jobs") or 0)
        if stuck > 0:
            result["stuck_jobs"] = bool(
                alerts.notify_event(
                    "stuck_jobs",
                    f"stuck_jobs:{scope}|hour={hour}",
                    f"Harness has {stuck} stuck job(s) on a dead worker",
                    detail={
                        "stuck_jobs": stuck,
                        "stuck_job_ids": list((health or {}).get("stuck_job_ids") or []),
                        "overdue_jobs": int((health or {}).get("overdue_jobs") or 0),
                    },
                    user_id=user_id,
                )
            )
        if worker.get("running") and _has_unrecovered_stale_alert():
            result["worker_recovered"] = bool(
                alerts.notify_event(
                    "worker_recovered",
                    f"worker_recovered|hour={hour}",
                    "Harness worker heartbeat fresh again — worker recovered",
                    detail={
                        "worker_id": worker.get("worker_id"),
                        "last_seen_at": worker.get("last_seen_at"),
                    },
                    user_id=user_id,
                )
            )
    except Exception:
        logger.exception("worker health alert check failed")
    return result
