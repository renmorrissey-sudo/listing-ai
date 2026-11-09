"""Durable job queue for the AI Harness (Phase 1).

Postgres/SQLite-backed, no Redis/Celery — same approach as
workers/sms_campaign_worker.py and tenant_sms_db.claim_next_job, with the
gaps fixed: stale-claim recovery (reclaim_stale_jobs) and a dead-letter
state after the retry budget is exhausted.

Every query is user_id-scoped where a user_id exists. All SQL uses ``?``
placeholders; db_backend.CompatConnection translates them to ``%s`` for
Postgres. Timestamps are UTC ISO strings. No new dependencies.

Tenant-scoping contract (M5/M6):

* Idempotency is scoped by ``(user_id, idempotency_key)`` (migration 030).
  A tenant reusing another tenant's key gets its own job, never the other
  tenant's job id; the same tenant reusing its own key still gets the
  original job id back.
* ``complete_job`` / ``fail_job`` accept an optional ``user_id``. When it
  is given, the mutation is ownership-checked in the UPDATE itself
  (``WHERE id = ? AND user_id = ?``): a cross-tenant call raises
  ``PermissionError`` and leaves the job untouched, an unknown id raises
  ``ValueError``. Any tenant-facing caller MUST pass ``user_id``.
* When ``user_id`` is omitted, the call is the authenticated-worker path:
  only the harness worker (``workers/harness_worker.py``) may call these
  functions without a tenant, and it only ever mutates jobs it has just
  claimed (it holds the full claimed row). That path exists so the worker
  and the existing unscoped call sites keep working; it must never be
  reached with a tenant-supplied job id. The worker should be updated to
  pass ``user_id=job["user_id"]`` so its mutations are ownership-checked
  too (follow-up outside this module).
* ``claim_jobs`` / ``reclaim_stale_jobs`` are intentionally worker-only
  bulk operations: the worker claims across tenants and dispatches each
  job through the per-tenant policy gates. They take no tenant input and
  must never be exposed to a tenant-facing route.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_BACKOFF_MINUTES = 120
# A claimed job is only reclaimable when its worker looks dead: the
# heartbeat freshness window matches the status API's 120 s window
# (harness.routes._harness_worker_heartbeat). A claim whose worker still
# has a fresh ``running`` heartbeat is left alone, even past the stale
# timeout -- otherwise a live worker running a job longer than the
# timeout would have its job reclaimed and double-executed.
HEARTBEAT_FRESH_SECONDS = 120

# --- Secret / PII redaction (H3) -------------------------------------------
# Durable harness rows must never hold verbatim secrets. Payloads are
# redacted at enqueue time, before anything is persisted: secret-like
# tokens anywhere in the payload, plus phone numbers / emails inside
# free-text body fields (the raw inbound body lives in sms_messages and
# is the source of truth -- the handler loads it by inbound_id instead).

_REDACTED = "[REDACTED]"

# Secret-like token shapes. `sk-...` covers OpenAI / Anthropic-style keys
# (sk-ant-..., sk-proj-..., sk-live/test-...); the assignment pattern
# covers `api_key=...` / `api-key: "..."` style leaks; the long-hex and
# long-opaque-token patterns catch bare hex/base64 secrets.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(?:ghp|gho|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-~+/=]{16,}"),
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password|auth[_-]?token"
        r"|private[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9._\-/+=]{8,}['\"]?"
    ),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
]

# Phone / email redaction applies ONLY to free-text body fields (see
# _BODY_KEYS): structured fields such as receiving_number keep the number
# because the send path needs it, and the lead's phone comes from the
# leads row, never from the payload.
_BODY_KEYS = frozenset(
    {"body", "inbound_body", "message_body", "message", "note", "notes",
     "reason", "text", "content"}
)
_PHONE_RE = re.compile(r"\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def redact_text(value, *, redact_pii=False):
    """Redact secret-like patterns in *value* (and phone/email when
    ``redact_pii`` is set, for free-text body fields). Non-strings pass
    through unchanged."""
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    if redact_pii:
        redacted = _EMAIL_RE.sub(_REDACTED, redacted)
        redacted = _PHONE_RE.sub(_REDACTED, redacted)
    return redacted


def redact_payload(payload):
    """Recursively redact a job payload dict before persistence.

    Secret-like tokens are redacted in every string value; phone numbers
    and emails are additionally redacted inside free-text body fields.
    Non-string leaves (ids, flags, nested dicts/lists) are preserved.
    """
    if isinstance(payload, dict):
        return {
            key: redact_payload(value)
            if isinstance(value, (dict, list))
            else redact_text(value, redact_pii=key in _BODY_KEYS)
            if isinstance(value, str)
            else value
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return dict(row) if row is not None else None


def _get_job_row(conn, job_id):
    return conn.execute(
        "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
    ).fetchone()


def new_correlation_id():
    """One identifier for tracing a single inbound event end to end.

    L4: the same value is carried from the inbound webhook through the
    job, the policy decision, the audit row, and the provider execution
    log, so one event can be followed across every surface. A uuid4 hex
    string (32 chars, no dashes) — never a secret, never PII.
    """
    return uuid.uuid4().hex


def enqueue_job(user_id, kind, payload, idempotency_key=None, correlation_id=None):
    """Insert a pending job and return its id.

    Idempotent per tenant: when idempotency_key is given and a job with
    that key already exists *for this user_id*, the existing job id is
    returned and no duplicate row is created. Idempotency is scoped by
    ``(user_id, idempotency_key)`` (migration 030): a different tenant
    reusing the same key gets a new job of its own, never the other
    tenant's job id. A concurrent duplicate insert (unique-key race) falls
    back to the same scoped lookup instead of raising.

    L4: ``correlation_id`` traces one inbound event through webhook,
    job, policy, audit, and provider execution. When the caller does not
    supply one, a fresh id is generated — every job carries one.

    The payload is redacted before persistence (see redact_payload): no
    verbatim secret ever lands in harness_jobs.payload_json. The raw
    inbound body is not needed here -- handlers load it from sms_messages
    by inbound_id; a redacted copy is kept only as a fallback for jobs
    whose inbound row is missing.
    """
    now = _now()
    correlation_id = correlation_id or new_correlation_id()
    payload_json = json.dumps(redact_payload(payload or {}))

    if idempotency_key is not None:
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM harness_jobs "
                "WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
        if existing:
            row = _row(existing)
            return row["id"]

    try:
        with db.get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO harness_jobs
                    (user_id, kind, payload_json, idempotency_key,
                     correlation_id, status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    user_id,
                    kind,
                    payload_json,
                    idempotency_key,
                    correlation_id,
                    now,
                    now,
                ),
            )
            return cur.lastrowid
    except Exception:
        # Unique-key race: another writer inserted the same key first.
        if idempotency_key is None:
            raise
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM harness_jobs "
                "WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
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

    Worker-authority by design (see module docstring): the worker claims
    across tenants and dispatches each job through the per-tenant policy
    gates. This function takes no tenant input and must never be exposed
    to a tenant-facing route.
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


def complete_job(job_id, result=None, *, user_id=None):
    """Mark a job succeeded, storing ``result`` (JSON-serializable) if given.

    Ownership (M5): when ``user_id`` is given, the UPDATE itself is scoped
    (``WHERE id = ? AND user_id = ?``), so a job owned by another tenant
    is never touched: the call raises ``PermissionError`` and the job is
    left exactly as it was. An unknown id raises ``ValueError`` (as
    before). When ``user_id`` is omitted, the call is the authenticated
    worker-authority path (see module docstring): only the harness worker
    may use it, and only for jobs it has just claimed. Tenant-facing
    callers must always pass ``user_id``.
    """
    now = _now()
    result_json = json.dumps(result) if result is not None else None
    with db.get_db() as conn:
        if user_id is None:
            cur = conn.execute(
                """
                UPDATE harness_jobs
                SET status = 'succeeded', result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (result_json, now, job_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"complete_job: unknown job id {job_id}")
            return
        cur = conn.execute(
            """
            UPDATE harness_jobs
            SET status = 'succeeded', result_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (result_json, now, job_id, user_id),
        )
        if cur.rowcount == 1:
            return
        # Nothing updated: distinguish "no such job" from "not your job"
        # without mutating anything.
        exists = conn.execute(
            "SELECT 1 FROM harness_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if exists is None:
            raise ValueError(f"complete_job: unknown job id {job_id}")
        raise PermissionError(
            f"complete_job: job {job_id} does not belong to user {user_id}"
        )


def fail_job(job_id, error, retryable=True, *, user_id=None):
    """Record a failure with exponential backoff, or dead-letter the job.

    Backoff: next_attempt_at = now + min(2**attempts, 120) minutes, where
    ``attempts`` is the post-claim count. After MAX_ATTEMPTS (5) attempts,
    or when retryable=False, status becomes 'dead_letter' and the job is
    never retried. The error is stored in result_json as {"error": ...}.

    Ownership (M5): same contract as ``complete_job`` — when ``user_id``
    is given, a job owned by another tenant raises ``PermissionError``
    and is left untouched (status, attempts and backoff unchanged); an
    unknown id raises ``ValueError``. When ``user_id`` is omitted, the
    call is the authenticated worker-authority path (see module
    docstring); tenant-facing callers must always pass ``user_id``.
    """
    now = _now()
    with db.get_db() as conn:
        if user_id is None:
            row = _row(_get_job_row(conn, job_id))
            if not row:
                raise ValueError(f"fail_job: unknown job id {job_id}")
        else:
            row = _row(
                conn.execute(
                    "SELECT * FROM harness_jobs "
                    "WHERE id = ? AND user_id = ?",
                    (job_id, user_id),
                ).fetchone()
            )
            if not row:
                exists = conn.execute(
                    "SELECT 1 FROM harness_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if exists is None:
                    raise ValueError(f"fail_job: unknown job id {job_id}")
                raise PermissionError(
                    f"fail_job: job {job_id} does not belong to user {user_id}"
                )

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
        if user_id is None:
            conn.execute(
                """
                UPDATE harness_jobs
                SET status = ?, next_attempt_at = ?, claimed_at = NULL,
                    claimed_by = NULL, result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, next_attempt_at, error_json, now, job_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE harness_jobs
                SET status = ?, next_attempt_at = ?, claimed_at = NULL,
                    claimed_by = NULL, result_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (status, next_attempt_at, error_json, now, job_id, user_id),
            )
            if cur.rowcount != 1:
                # The job was read scoped above, so a 0 here means it was
                # deleted or reassigned between the read and the write.
                # Refuse to report success rather than mutate blindly.
                raise RuntimeError(
                    f"fail_job: job {job_id} changed during failure handling"
                )
    return status


def reclaim_stale_jobs(timeout_seconds=300):
    """Move jobs left 'claimed'/'running' past the timeout back to pending.

    Returns the number of jobs reclaimed. Clears claimed_at/claimed_by and
    next_attempt_at so the job is immediately due; ``attempts`` is left
    untouched so the retry budget still applies. A crashed worker's jobs
    are never silently lost — this is the recovery the SMS campaign worker
    lacks.

    Exactly-once guard: a stale claim is NOT reclaimed while its worker
    is demonstrably alive — i.e. ``claimed_by`` matches a
    ``sms_worker_heartbeats`` row with ``status='running'`` and a
    ``last_seen_at`` within the last ``HEARTBEAT_FRESH_SECONDS`` (120 s,
    the same window the status API uses). Without this, a live worker
    running a job longer than ``timeout_seconds`` would have its job
    reclaimed mid-execution and the job would run twice. A dead worker's
    heartbeat goes stale within 120 s, so its jobs are still reclaimed
    once the claim itself is past the timeout. Claims with no heartbeat
    row at all (crashed before the first heartbeat, test fixtures) are
    reclaimed as before. If the heartbeat table is missing (migration
    016 not applied), the guard is skipped and the plain reclaim runs.

    Worker-authority by design (see module docstring): takes no tenant
    input and must never be exposed to a tenant-facing route.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    ).isoformat()
    fresh_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_FRESH_SECONDS)
    ).isoformat()
    now = _now()
    guarded_sql = """
        UPDATE harness_jobs
        SET status = 'pending', claimed_at = NULL, claimed_by = NULL,
            next_attempt_at = NULL, updated_at = ?
        WHERE status IN ('claimed', 'running')
          AND claimed_at IS NOT NULL
          AND claimed_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM sms_worker_heartbeats h
              WHERE h.worker_id = harness_jobs.claimed_by
                AND h.status = 'running'
                AND h.last_seen_at >= ?
          )
    """
    plain_sql = """
        UPDATE harness_jobs
        SET status = 'pending', claimed_at = NULL, claimed_by = NULL,
            next_attempt_at = NULL, updated_at = ?
        WHERE status IN ('claimed', 'running')
          AND claimed_at IS NOT NULL
          AND claimed_at <= ?
    """
    try:
        with db.get_db() as conn:
            cur = conn.execute(guarded_sql, (now, cutoff, fresh_cutoff))
            return int(cur.rowcount or 0)
    except Exception:
        # Heartbeat table missing (or any guard failure): fall back to
        # the plain reclaim rather than leaving crashed jobs stranded.
        logger.warning(
            "reclaim_stale_jobs: live-worker guard unavailable; "
            "falling back to unguarded reclaim"
        )
    with db.get_db() as conn:
        cur = conn.execute(plain_sql, (now, cutoff))
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
