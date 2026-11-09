"""General harness job worker (Phase 1, no Redis/Celery).

Polls the durable Postgres/SQLite ``harness_jobs`` queue (see plan SS4.4)
and dispatches by job kind. Reuses the SMS campaign worker's claim / retry
/ heartbeat patterns, with its two known gaps fixed:

1. Stale-claim recovery: every loop reclaims jobs left ``claimed`` /
   ``running`` past the timeout via ``harness.jobs.reclaim_stale_jobs``,
   so a killed worker never loses work silently.
2. ``failed_retryable`` is never terminal: a retryable failure is
   re-queued by ``fail_job(..., retryable=True)`` with a future
   ``next_attempt_at``. Only ``fail_job(..., retryable=False)`` lands a job
   in ``dead_letter``. This worker has no campaign-completion aggregation
   that could mistake retryable jobs for finished ones.

Job kinds: ``sms_auto_reply``, ``scheduled_follow_up`` and
``execute_approval``. Unknown kinds fail with ``retryable=False`` so
they land in ``dead_letter`` -- never silently dropped.

Liveness: the worker writes a durable heartbeat row (``sms_worker_heartbeats``,
worker_id ``harness-<hostname>-<pid>``) at boot, every loop iteration,
and ``stopped`` on clean shutdown -- plus a background thread that
heartbeats every 10 s so a long dispatch cannot let the row go stale --
so a running/stale worker is detectable by a DB query and surfaced in
``GET /api/harness/status`` (``worker`` key) -- not just by grepping
Railway logs. The SMS campaign worker writes the same table with
unprefixed ``<hostname>-<pid>`` ids, so the two workers' rows never
collide. Stale-claim recovery skips jobs claimed by a worker whose
heartbeat is still fresh (see ``harness.jobs.reclaim_stale_jobs``), so
a live worker running a long job can never have its job reclaimed and
double-executed.

``harness.jobs`` is owned by the core harness package and may not exist
yet; it is imported lazily and defensively, so this worker still starts
(and logs + sleeps/retries) when the tables/module are missing.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone

import db

# Staging safety: the boot guard (live Stripe key, production Telnyx
# number, real provider keys without mock/trial, email/voice/realtime
# keys in staging) must run in the worker too, not just the web
# process — the worker is the process that executes approved sends.
# validate_config() exits(1) on a bad config, before any DB work.
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harness_worker")

_RUNNING = True
CLAIM_LIMIT = 10
HEARTBEAT_EVERY_LOOPS = 30
# Background-heartbeat interval. The main loop heartbeats between batches,
# but a long dispatch blocks the loop, so without this a job running
# longer than the 120 s status freshness window would make a live worker
# look dead. 10 s keeps the heartbeat far inside that window.
HEARTBEAT_THREAD_INTERVAL_SECONDS = 10

_jobs_module = None
_jobs_missing_logged = False


def _handle_signal(signum, frame):
    global _RUNNING
    logger.info("Shutdown signal received (%s)", signum)
    _RUNNING = False


def _now():
    return datetime.now(timezone.utc)


def _alert_dead_letter(job, error):
    """M4: notify once when a job lands in dead_letter. Never raises --
    harness.alerts is itself non-blocking, and this is wrapped too, so
    a failing notifier can never break job settlement."""
    try:
        from harness import alerts

        alerts.alert_dead_letter(job, error)
    except Exception:
        logger.exception("dead-letter alert failed job=%s", (job or {}).get("id"))


def _alert_provider_failure(reason, kind, job):
    """M4: count a retryable handler failure toward the provider-failure
    threshold (alerts at 3 consecutive failures per provider/signature).
    Never raises."""
    try:
        from harness import alerts

        alerts.alert_provider_failure(
            alerts.detect_provider(reason),
            reason,
            kind=kind,
            user_id=(job or {}).get("user_id"),
            correlation_id=(job or {}).get("correlation_id"),
        )
    except Exception:
        logger.exception("provider-failure alert failed kind=%s", kind)


def _alert_worker_failure(error, context=None, correlation_id=None):
    """M4: notify once per error signature per hour when the worker
    itself fails. Never raises."""
    try:
        from harness import alerts

        alerts.alert_worker_failure(
            error, context=context, correlation_id=correlation_id
        )
    except Exception:
        logger.exception("worker-failure alert failed")


def _touch_heartbeat(worker_id, status, metadata=None):
    """Durable liveness signal for the harness worker. Never raises.

    Writes one upserted row in ``sms_worker_heartbeats`` (migration 016,
    shared with the SMS campaign worker) so a running worker is
    detectable by a DB query -- not just by grepping Railway logs:

        SELECT worker_id, status, last_seen_at
          FROM sms_worker_heartbeats WHERE worker_id LIKE 'harness-%';

    Worker ids are ``harness-<hostname>-<pid>`` (see main()), so harness
    rows never collide with the SMS campaign worker's
    ``<hostname>-<pid>`` rows. ``harness.routes.get_harness_status``
    reads the freshest running row back into the ``worker`` status key.
    """
    try:
        import tenant_sms_db as tdb

        meta = {"service": "harness_worker"}
        if metadata:
            meta.update(metadata)
        tdb.touch_worker_heartbeat(worker_id, status=status, metadata=meta)
    except Exception:
        logger.exception("harness worker heartbeat failed id=%s", worker_id)


def _start_heartbeat_thread(worker_id, interval_seconds=HEARTBEAT_THREAD_INTERVAL_SECONDS):
    """Start a daemon thread that heartbeats every ``interval_seconds``.

    The main loop heartbeats between batches, but a single long dispatch
    (a slow AI call) blocks the loop for the whole dispatch. This thread
    keeps the durable liveness row fresh during long jobs, so
    ``GET /api/harness/status`` keeps reporting ``worker.running=true``
    while the worker is actually working. Returns ``(stop_event,
    thread)``; call ``stop_event.set()`` and join the thread on shutdown.
    The heartbeat write itself never raises (see _touch_heartbeat), so
    this thread can never break job processing.
    """
    stop_event = threading.Event()

    def _beat():
        while not stop_event.wait(interval_seconds):
            _touch_heartbeat(worker_id, "running")

    thread = threading.Thread(
        target=_beat, name=f"harness-heartbeat-{worker_id}", daemon=True
    )
    thread.start()
    return stop_event, thread


def _load_jobs_module():
    """Lazily import harness.jobs; None (with a warning) if unavailable."""
    global _jobs_module, _jobs_missing_logged
    if _jobs_module is not None:
        return _jobs_module
    try:
        import importlib

        _jobs_module = importlib.import_module("harness.jobs")
        return _jobs_module
    except Exception:
        if not _jobs_missing_logged:
            logger.warning(
                "harness.jobs unavailable (module/tables not present yet); "
                "worker will idle and retry"
            )
            _jobs_missing_logged = True
        return None


def _reclaim_stale(jobs):
    """Reclaim stale claimed/running jobs. Returns the reclaimed count or 0."""
    reclaim = getattr(jobs, "reclaim_stale_jobs", None)
    if reclaim is None:
        return 0
    try:
        result = reclaim()
        return int(result or 0)
    except TypeError:
        try:
            result = reclaim(timeout_seconds=300)
            return int(result or 0)
        except Exception:
            logger.exception("reclaim_stale_jobs failed")
            return 0
    except Exception:
        logger.exception("reclaim_stale_jobs failed")
        return 0


def _claim_jobs(jobs, limit, claimed_by="harness_worker"):
    """Claim up to ``limit`` pending jobs. Returns a list of job dicts."""
    for name in ("claim_jobs", "claim_pending_jobs"):
        claim = getattr(jobs, name, None)
        if claim is None:
            continue
        try:
            result = claim(limit=limit, claimed_by=claimed_by)
        except TypeError:
            try:
                result = claim(limit=limit)
            except TypeError:
                try:
                    result = claim(limit)
                except Exception:
                    logger.exception("%s failed", name)
                    return []
            except Exception:
                logger.exception("%s failed", name)
                return []
        except Exception:
            logger.exception("%s failed", name)
            return []
        if not result:
            return []
        return list(result) if isinstance(result, (list, tuple)) else [result]
    claim_one = getattr(jobs, "claim_job", None)
    if claim_one is None:
        logger.warning("harness.jobs exposes no claim function; idling")
        return []
    claimed = []
    for _ in range(limit):
        try:
            job = claim_one()
        except Exception:
            logger.exception("claim_job failed")
            break
        if not job:
            break
        claimed.append(job)
    return claimed


def _complete_job(jobs, job, result):
    job_id = job.get("id")
    complete = getattr(jobs, "complete_job", None)
    if complete is None:
        logger.warning("harness.jobs exposes no complete_job; job=%s", job_id)
        return
    try:
        # M5: worker mutations are ownership-checked with the claimed job's
        # tenant (the worker is trusted, but the check must still hold).
        complete(job_id, result, user_id=job.get("user_id"))
    except TypeError:
        complete(job_id, result)
    except Exception:
        logger.exception("complete_job failed job=%s", job_id)


def _fail_job(jobs, job, error, *, retryable):
    """Fail a job. retryable=False must land it in dead_letter (jobs module
    contract); retryable=True re-queues it with backoff -- never terminal.
    A dead-letter transition triggers the M4 ops alert."""
    job_id = job.get("id")
    fail = getattr(jobs, "fail_job", None)
    if fail is None:
        logger.warning("harness.jobs exposes no fail_job; job=%s", job_id)
        return
    status = None
    try:
        # M5: worker mutations are ownership-checked with the claimed job's
        # tenant (the worker is trusted, but the check must still hold).
        status = fail(job_id, error, retryable=retryable, user_id=job.get("user_id"))
    except TypeError:
        try:
            status = fail(job_id, error, retryable=retryable)
        except Exception:
            logger.exception("fail_job failed job=%s", job_id)
            return
    except Exception:
        logger.exception("fail_job failed job=%s", job_id)
        return
    if status == "dead_letter":
        _alert_dead_letter(job, error)


def _job_payload(job):
    payload = (job or {}).get("payload")
    if payload is None:
        payload = (job or {}).get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _run_known_handler(jobs, job, handler, kind):
    """Run one known-kind handler and settle the job by its ok/retryable
    contract: ok -> complete; ok=False + retryable -> re-queue with
    backoff; ok=False + not retryable -> dead-letter."""
    job_id = (job or {}).get("id")
    try:
        result = handler(job) or {}
    except Exception as exc:
        logger.exception("%s handler crashed job=%s", kind, job_id)
        _alert_provider_failure(f"handler_error: {exc}"[:500], kind, job)
        _fail_job(jobs, job, f"handler_error: {exc}"[:500], retryable=True)
        return True
    if result.get("ok"):
        _complete_job(jobs, job, result)
    elif result.get("retryable"):
        reason = str(result.get("reason") or "handler_failed")[:500]
        _alert_provider_failure(reason, kind, job)
        _fail_job(
            jobs,
            job,
            reason,
            retryable=True,
        )
    else:
        _fail_job(
            jobs,
            job,
            str(result.get("reason") or "handler_failed")[:500],
            retryable=False,
        )
    return True


def dispatch_job(job):
    """Dispatch one claimed job. Returns True if the job reached a terminal
    outcome (completed or dead-lettered) or was re-queued retryably."""
    kind = (job or {}).get("kind")
    job_id = (job or {}).get("id")
    jobs = _load_jobs_module()
    if jobs is None:
        return False
    if kind == "sms_auto_reply":
        from harness.sms_handler import process_sms_auto_reply_job

        return _run_known_handler(jobs, job, process_sms_auto_reply_job, kind)
    if kind == "scheduled_follow_up":
        from harness.followup import process_scheduled_follow_up_job

        return _run_known_handler(jobs, job, process_scheduled_follow_up_job, kind)
    if kind == "execute_approval":
        from harness.approvals import process_execute_approval_job

        return _run_known_handler(jobs, job, process_execute_approval_job, kind)
    logger.error("Unknown harness job kind=%s job=%s; dead-lettering", kind, job_id)
    _fail_job(jobs, job, f"unknown_kind: {kind}"[:500], retryable=False)
    return True


def process_one_batch(worker_id: str, limit: int = CLAIM_LIMIT) -> bool:
    """One worker iteration: reclaim stale claims, then claim + dispatch up
    to ``limit`` pending jobs. Returns True if any work ran."""
    jobs = _load_jobs_module()
    if jobs is None:
        return False
    try:
        reclaimed = _reclaim_stale(jobs)
        if reclaimed:
            logger.info("Harness worker reclaimed %s stale job(s)", reclaimed)
    except Exception:
        logger.exception("Stale-claim reclaim failed")
    claimed = _claim_jobs(jobs, limit, claimed_by=worker_id)
    if not claimed:
        return False
    worked = False
    max_attempts = int(getattr(jobs, "MAX_ATTEMPTS", 5) or 5)
    for job in claimed:
        worked = True
        # Budget-exhaustion escape: attempts are counted at claim time,
        # so a job that was repeatedly reclaimed after crashes can arrive
        # here already over budget without ever having been dispatched
        # (fail_job only runs after a dispatch). The claim that spends
        # the 5th attempt is still a legitimate dispatch — a retryable
        # failure there dead-letters via fail_job — so the guard fires
        # only strictly over budget (a 6th+ claim). Dead-lettering here
        # fires the M4 alert, so a crash-looping job is visible rather
        # than silently re-queued.
        if int(job.get("attempts") or 0) > max_attempts:
            logger.warning(
                "Harness worker dead-lettering budget-exhausted job=%s attempts=%s",
                job.get("id"),
                job.get("attempts"),
            )
            try:
                _fail_job(
                    jobs,
                    job,
                    "attempts_exhausted",
                    retryable=False,
                )
            except Exception:
                pass
            continue
        try:
            dispatch_job(job)
        except Exception as exc:
            logger.exception("dispatch_job crashed job=%s", job.get("id"))
            _alert_worker_failure(
                f"dispatch_error job={job.get('id')}: {exc}"[:500],
                context={"job_id": job.get("id"), "kind": job.get("kind")},
                correlation_id=job.get("correlation_id"),
            )
            try:
                _fail_job(jobs, job, "dispatch_error", retryable=True)
            except Exception:
                pass
    return worked


def run_once(worker_id: str = "harness-worker-test", limit: int = CLAIM_LIMIT) -> bool:
    """A single iteration (reclaim + claim + dispatch), for tests/smoke."""
    return process_one_batch(worker_id, limit=limit)


def main():
    # Fail closed on a bad staging/production config before touching
    # the queue (see config.validate_staging_safety). Process-aware:
    # this worker performs no Stripe operations, so it does not require
    # Stripe credentials (see config._STRIPE_REQUIRED_PROCESSES).
    config.validate_config(process="harness-worker")
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    worker_id = f"harness-{socket.gethostname()}-{os.getpid()}"
    logger.info("Harness worker starting id=%s", worker_id)
    try:
        db.init_db()
        logger.info("Harness worker schema ready id=%s", worker_id)
    except Exception:
        logger.exception(
            "Harness worker db.init_db failed; continuing (jobs module may be absent)"
        )
    _touch_heartbeat(worker_id, "running", {"boot": True})
    hb_stop, hb_thread = _start_heartbeat_thread(worker_id)
    idle_sleep = 2
    loops = 0
    while _RUNNING:
        try:
            loops += 1
            _touch_heartbeat(worker_id, "running")
            if loops % HEARTBEAT_EVERY_LOOPS == 1:
                logger.info(
                    "Harness worker heartbeat loop=%s id=%s running=%s",
                    loops,
                    worker_id,
                    _RUNNING,
                )
            worked = process_one_batch(worker_id)
            if worked:
                idle_sleep = 1
            else:
                time.sleep(idle_sleep)
                idle_sleep = min(idle_sleep + 1, 10)
        except Exception as exc:
            logger.exception("Harness worker loop error")
            _alert_worker_failure(f"worker_loop_error: {exc}"[:500])
            time.sleep(5)
    hb_stop.set()
    hb_thread.join(timeout=5)
    _touch_heartbeat(worker_id, "stopped")
    logger.info("Harness worker stopped cleanly id=%s", worker_id)


if __name__ == "__main__":
    main()
