"""Durable job queue for the AI Harness (Phase 1).

Postgres/SQLite-backed, no Redis/Celery — same approach as
workers/sms_campaign_worker.py and tenant_sms_db.claim_next_job, with the
gaps fixed: stale-claim recovery (reclaim_stale_jobs) and a dead-letter
state after the retry budget is exhausted.

Every query is user_id-scoped where a user_id exists. All SQL uses ``?``
placeholders; db_backend.CompatConnection translates them to ``%s`` for
Postgres. Timestamps are UTC ISO strings. No new dependencies.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import db

MAX_ATTEMPTS = 5
MAX_BACKOFF_MINUTES = 120


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return dict(row) if row is not None else None


def _get_job_row(conn, job_id):
    return conn.execute(
        "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
    ).fetchone()


def enqueue_job(user_id, kind, payload, idempotency_key=None):
    """Insert a pending job and return its id.

    Idempotent: when idempotency_key is given and a job with that key
    already exists, the existing job id is returned and no duplicate row
    is created. A concurrent duplicate insert (unique-key race) falls back
    to the same lookup instead of raising.
    """
    now = _now()
    payload_json = json.dumps(payload or {})

    if idempotency_key is not None:
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM harness_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if existing:
            row = _row(existing)
            return row["id"]

    try:
        with db.get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO harness_jobs
                    (user_id, kind, payload_json, idempotency_key, status,
                     attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (user_id, kind, payload_json, idempotency_key, now, now),
            )
            return cur.lastrowid
    except Exception:
        # Unique-key race: another writer inserted the same key first.
        if idempotency_key is None:
            raise
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM harness_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if existing:
            return _row(existing)["id"]
        raise


def _claim_one_postgres(claimed_by, now):
    """Claim a single due pending job (Postgres: FOR UPDATE SKIP LOCKED)."""
    with db.get_db() as conn:
        row = conn.execute(
            """
            UPDATE harness_jobs
            SET status = 'claimed', claimed_at = ?, claimed_by = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE id = (
                SELECT j.id FROM harness_jobs j
                WHERE j.status = 'pending'
                  AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?)
                ORDER BY j.id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *
            """,
            (now, claimed_by, now, now),
        ).fetchone()
        return _row(row)


def _claim_one_sqlite(claimed_by, now):
    """Claim a single due pending job (SQLite: SELECT + guarded UPDATE)."""
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM harness_jobs
            WHERE status = 'pending'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return None
        job_id = _row(row)["id"]
        cur = conn.execute(
            """
            UPDATE harness_jobs
            SET status = 'claimed', claimed_at = ?, claimed_by = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now, claimed_by, now, job_id),
        )
        if cur.rowcount != 1:
            return None  # Lost a race with another worker; not claimed.
        return _row(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def claim_jobs(limit=10, claimed_by="harness_worker", stale_timeout_seconds=300):
    """Claim up to ``limit`` due pending jobs and return them as dicts.

    Claiming sets status='claimed', records claimed_at/claimed_by, and
    increments attempts (the retry budget is counted at claim time, as in
    the SMS campaign worker). Only jobs whose next_attempt_at is NULL or
    in the past are due. ``stale_timeout_seconds`` is accepted for
    signature symmetry with reclaim_stale_jobs; stale-claim recovery is
    a separate explicit step, not a side effect of claiming.
    """
    import config

    now = _now()
    claimed = []
    for _ in range(max(int(limit), 0)):
        if config.DB_ENGINE == "postgres":
            job = _claim_one_postgres(claimed_by, now)
        else:
            job = _claim_one_sqlite(claimed_by, now)
        if not job:
            break
        claimed.append(job)
    return claimed


def complete_job(job_id, result=None):
    """Mark a job succeeded, storing ``result`` (JSON-serializable) if given."""
    now = _now()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            UPDATE harness_jobs
            SET status = 'succeeded', result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(result) if result is not None else None, now, job_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"complete_job: unknown job id {job_id}")


def fail_job(job_id, error, retryable=True):
    """Record a failure with exponential backoff, or dead-letter the job.

    Backoff: next_attempt_at = now + min(2**attempts, 120) minutes, where
    ``attempts`` is the post-claim count. After MAX_ATTEMPTS (5) attempts,
    or when retryable=False, status becomes 'dead_letter' and the job is
    never retried. The error is stored in result_json as {"error": ...}.
    """
    now = _now()
    with db.get_db() as conn:
        row = _row(_get_job_row(conn, job_id))
    if not row:
        raise ValueError(f"fail_job: unknown job id {job_id}")

    attempts = int(row.get("attempts") or 0)
    error_json = json.dumps({"error": str(error)[:500]})
    if not retryable or attempts >= MAX_ATTEMPTS:
        status = "dead_letter"
        next_attempt_at = None
    else:
        status = "pending"
        delay = min(2 ** attempts, MAX_BACKOFF_MINUTES)
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(minutes=delay)
        ).isoformat()

    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_jobs
            SET status = ?, next_attempt_at = ?, claimed_at = NULL,
                claimed_by = NULL, result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, next_attempt_at, error_json, now, job_id),
        )
    return status


def reclaim_stale_jobs(timeout_seconds=300):
    """Move jobs left 'claimed'/'running' past the timeout back to pending.

    Returns the number of jobs reclaimed. Clears claimed_at/claimed_by and
    next_attempt_at so the job is immediately due; ``attempts`` is left
    untouched so the retry budget still applies. A crashed worker's jobs
    are never silently lost — this is the recovery the SMS campaign worker
    lacks.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    ).isoformat()
    now = _now()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            UPDATE harness_jobs
            SET status = 'pending', claimed_at = NULL, claimed_by = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE status IN ('claimed', 'running')
              AND claimed_at IS NOT NULL
              AND claimed_at <= ?
            """,
            (now, cutoff),
        )
        return int(cur.rowcount or 0)


def get_job(job_id, user_id):
    """Return one job dict, scoped to ``user_id``; None if not found/owned."""
    with db.get_db() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
        )


def list_jobs(user_id, status=None, limit=50):
    """List a tenant's jobs, newest first, optionally filtered by status."""
    with db.get_db() as conn:
        if status is None:
            rows = conn.execute(
                """
                SELECT * FROM harness_jobs
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM harness_jobs
                WHERE user_id = ? AND status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, status, limit),
            ).fetchall()
    return [_row(r) for r in rows]
