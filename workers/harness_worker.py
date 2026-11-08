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

Job kinds in Phase 1: ``sms_auto_reply`` only. Unknown kinds fail with
``retryable=False`` so they land in ``dead_letter`` -- never silently
dropped.

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
import time
from datetime import datetime, timezone

import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harness_worker")

_RUNNING = True
CLAIM_LIMIT = 10
HEARTBEAT_EVERY_LOOPS = 30

_jobs_module = None
_jobs_missing_logged = False


def _handle_signal(signum, frame):
    global _RUNNING
    logger.info("Shutdown signal received (%s)", signum)
    _RUNNING = False


def _now():
    return datetime.now(timezone.utc)


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
        complete(job_id, result)
    except TypeError:
        complete(job_id)
    except Exception:
        logger.exception("complete_job failed job=%s", job_id)


def _fail_job(jobs, job, error, *, retryable):
    """Fail a job. retryable=False must land it in dead_letter (jobs module
    contract); retryable=True re-queues it with backoff -- never terminal."""
    job_id = job.get("id")
    fail = getattr(jobs, "fail_job", None)
    if fail is None:
        logger.warning("harness.jobs exposes no fail_job; job=%s", job_id)
        return
    try:
        fail(job_id, error, retryable=retryable)
    except TypeError:
        try:
            fail(job_id, error)
        except Exception:
            logger.exception("fail_job failed job=%s", job_id)
    except Exception:
        logger.exception("fail_job failed job=%s", job_id)


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


def dispatch_job(job):
    """Dispatch one claimed job. Returns True if the job reached a terminal
    outcome (completed or dead-lettered) or was re-queued retryably."""
    kind = (job or {}).get("kind")
    job_id = (job or {}).get("id")
    jobs = _load_jobs_module()
    if jobs is None:
        return False
    if kind == "sms_auto_reply":
        try:
            from harness.sms_handler import process_sms_auto_reply_job

            result = process_sms_auto_reply_job(job) or {}
        except Exception as exc:
            logger.exception("sms_auto_reply handler crashed job=%s", job_id)
            _fail_job(jobs, job, f"handler_error: {exc}"[:500], retryable=True)
            return True
        if result.get("ok"):
            _complete_job(jobs, job, result)
        elif result.get("retryable"):
            _fail_job(
                jobs,
                job,
                str(result.get("reason") or "handler_failed")[:500],
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
    for job in claimed:
        worked = True
        try:
            dispatch_job(job)
        except Exception:
            logger.exception("dispatch_job crashed job=%s", job.get("id"))
            try:
                _fail_job(jobs, job, "dispatch_error", retryable=True)
            except Exception:
                pass
    return worked


def run_once(worker_id: str = "harness-worker-test", limit: int = CLAIM_LIMIT) -> bool:
    """A single iteration (reclaim + claim + dispatch), for tests/smoke."""
    return process_one_batch(worker_id, limit=limit)


def main():
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
    idle_sleep = 2
    loops = 0
    while _RUNNING:
        try:
            loops += 1
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
        except Exception:
            logger.exception("Harness worker loop error")
            time.sleep(5)
    logger.info("Harness worker stopped cleanly id=%s", worker_id)


if __name__ == "__main__":
    main()
