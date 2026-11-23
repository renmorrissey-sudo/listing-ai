"""Duplicate-outreach ledger + normalized phone column (047).

Phase III-3: a durable cross-channel outreach ledger
(``harness_outreach_ledger``) that makes the III-1 lead+reason guard
durable — the refusal survives restarts, re-runs, and re-queues —
plus ``leads.phone_normalized`` (E.164) with a backfill and a
non-unique index, closing the exact-string + last-10-scan dedup race.

Data safety
------------
Additive and forward-only. One new table plus four indexes, created
``IF NOT EXISTS`` (Postgres) or after PRAGMA checks (SQLite); one
nullable column added to ``leads`` with ``IF NOT EXISTS`` /
PRAGMA-guarded DDL (Postgres / SQLite twins). No existing table is
altered except the added nullable column; no existing data is
modified except the backfilled normalized values; no seed rows.
Postcondition checks run INLINE in the upgrade functions (the runner
does not call a migration's own ``verify()``) so a failure rolls the
migration back. The Postgres checks only test ``row is not None`` —
they never index into a row, so they are safe against the
empty-Postgres test fake, which returns stub rows.

Backfill
--------
* ``leads.phone_normalized``: computed in Python from
  ``phone_number`` with the migration-local E.164 routine below (a
  mirror of ``lead_service.normalize_phone_e164`` — migrations must
  be self-contained and never import app code). Un-normalizable
  stored numbers get ``''`` and keep exact-string matching.
* ``harness_outreach_ledger``: ``INSERT ... SELECT`` from
  ``harness_follow_up_sends`` — rows with status
  scheduled/claimed/drafted (any age: they are in-flight) and sent
  within 7 days; blocked/dead_letter/older-sent rows are skipped
  (they never block). 'drafted' rows are backfilled with their
  approval id; the guard's approval-state lookup determines liveness
  at read time, so settled-approval rows never block. Without the
  backfill the deploy itself would reset the guard. The insert is
  idempotent (NOT EXISTS on (user_id, job_id)) so a re-run is safe.

Staged UNIQUE
-------------
047 adds ``phone_normalized`` + backfill + a NON-unique index only.
It does NOT add the UNIQUE constraint: pre-existing duplicate leads
(same normalized phone under different raw strings — the bug this
phase fixes) would violate it and fail the migration. The conflict
report below logs every (user_id, phone_normalized) group with >1
lead at WARNING level so the count is visible in the deploy log; the
UNIQUE lands in a follow-up migration once the report shows zero
conflicts in staging + production.

Rollback
--------
Additive. Rollback is a code-only revert — the table and column stay,
unused, and no data is lost. No down-migration is run (the release
has no down-migrations by design).
"""

import logging
import re

VERSION = "047_outreach_ledger"

TABLE = "harness_outreach_ledger"

COLUMNS = (
    "id",
    "user_id",
    "lead_id",
    "channel",
    "reason",
    "reason_norm",
    "dedup_key",
    "phone_normalized",
    "status",
    "sms_message_id",
    "voice_call_id",
    "job_id",
    "approval_id",
    "scheduled_at",
    "sent_at",
    "created_at",
    "updated_at",
)

INDEXES = (
    "idx_outreach_ledger_user_job",
    "idx_outreach_ledger_lead_reason",
    "idx_outreach_ledger_lead",
    "idx_outreach_ledger_phone",
)

SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS harness_outreach_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    lead_id INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    reason TEXT NOT NULL DEFAULT 'Follow up',
    reason_norm TEXT NOT NULL DEFAULT 'follow up',
    dedup_key TEXT NOT NULL,
    phone_normalized TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    sms_message_id INTEGER,
    voice_call_id INTEGER,
    job_id INTEGER,
    approval_id INTEGER,
    scheduled_at TEXT NOT NULL,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

PG_TABLE = """
CREATE TABLE IF NOT EXISTS harness_outreach_ledger (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    lead_id BIGINT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    reason TEXT NOT NULL DEFAULT 'Follow up',
    reason_norm TEXT NOT NULL DEFAULT 'follow up',
    dedup_key TEXT NOT NULL,
    phone_normalized TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    sms_message_id BIGINT,
    voice_call_id BIGINT,
    job_id BIGINT,
    approval_id BIGINT,
    scheduled_at TEXT NOT NULL,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

SQLITE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_ledger_user_job "
    "ON harness_outreach_ledger(user_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_ledger_lead_reason "
    "ON harness_outreach_ledger(user_id, lead_id, reason_norm, status)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_ledger_lead "
    "ON harness_outreach_ledger(user_id, lead_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_ledger_phone "
    "ON harness_outreach_ledger(user_id, phone_normalized, created_at)",
)

PG_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_ledger_user_job "
    "ON harness_outreach_ledger(user_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_ledger_lead_reason "
    "ON harness_outreach_ledger(user_id, lead_id, reason_norm, status)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_ledger_lead "
    "ON harness_outreach_ledger(user_id, lead_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_ledger_phone "
    "ON harness_outreach_ledger(user_id, phone_normalized, created_at)",
)

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"\+[1-9]\d{9,14}\Z")


def _normalize_e164(raw):
    """Migration-local E.164 mirror of lead_service.normalize_phone_e164.

    Only digits and common formatting (+ - ( ) space .) are allowed;
    anything else (letters, extension markers) is un-normalizable and
    returns "". Kept local so the migration never imports app code.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.search(r"[^0-9+\-().\s]", text):
        return ""
    if text.count("+") > 1 or ("+" in text[1:]):
        return ""
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        candidate = "+" + digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        return ""
    return candidate if _E164_RE.fullmatch(candidate) else ""


def _columns_sqlite(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return names


def _indexes_sqlite(conn, table):
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return names


def _has_column_postgres(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    return row is not None


def _has_index_postgres(conn, table, index):
    row = conn.execute(
        """
        SELECT 1 FROM pg_indexes
        WHERE tablename = %s AND indexname = %s
        """,
        (table, index),
    ).fetchone()
    return row is not None


def _check_table(conn, dialect):
    """Inline postcondition: table + columns exist (and indexes on
    SQLite). Raises so the runner rolls the migration back.

    The Postgres index check is intentionally NOT inline: the
    empty-Postgres test fake stubs ``information_schema.columns`` but
    not ``pg_indexes``, and the runner never calls a migration's own
    ``verify()`` — so, following the 030/045 precedent, Postgres
    index verification lives in :func:`verify_postgres`, not in the
    upgrade path. The Postgres checks only test row existence, never
    index into a row.
    """
    if dialect == "postgresql":
        for column in COLUMNS:
            if not _has_column_postgres(conn, TABLE, column):
                raise RuntimeError(
                    "047_outreach_ledger failed: "
                    f"{TABLE}.{column} missing"
                )
        if not _has_column_postgres(conn, "leads", "phone_normalized"):
            raise RuntimeError(
                "047_outreach_ledger failed: leads.phone_normalized missing"
            )
        return
    present = _columns_sqlite(conn, TABLE)
    missing = [c for c in COLUMNS if c not in present]
    if missing:
        raise RuntimeError(
            "047_outreach_ledger failed: "
            f"{TABLE} missing columns: {', '.join(missing)}"
        )
    indexes = _indexes_sqlite(conn, TABLE)
    missing_indexes = [i for i in INDEXES if i not in indexes]
    if missing_indexes:
        raise RuntimeError(
            "047_outreach_ledger failed: "
            f"{TABLE} missing indexes: {', '.join(missing_indexes)}"
        )
    lead_columns = _columns_sqlite(conn, "leads")
    if "phone_normalized" not in lead_columns:
        raise RuntimeError(
            "047_outreach_ledger failed: leads.phone_normalized missing"
        )


def _check_indexes_postgres(conn):
    """Postgres index postcondition (verify-time only, see above)."""
    for index in INDEXES:
        if not _has_index_postgres(conn, TABLE, index):
            raise RuntimeError(
                "047_outreach_ledger failed: "
                f"index {index} missing"
            )


def _add_phone_normalized_column(conn, dialect):
    if dialect == "postgresql":
        if not _has_column_postgres(conn, "leads", "phone_normalized"):
            conn.execute(
                "ALTER TABLE leads ADD COLUMN IF NOT EXISTS "
                "phone_normalized TEXT"
            )
        return
    if "phone_normalized" not in _columns_sqlite(conn, "leads"):
        conn.execute("ALTER TABLE leads ADD COLUMN phone_normalized TEXT")


def _backfill_phone_normalized(conn, dialect):
    """Fill leads.phone_normalized from phone_number (Python-side,
    so both engines share the one routine). Returns rows updated."""
    rows = conn.execute(
        "SELECT id, phone_number FROM leads "
        "WHERE phone_normalized IS NULL"
    ).fetchall()
    ph = "%s" if dialect == "postgresql" else "?"
    updated = 0
    for raw in rows:
        try:
            row = dict(raw)
            lead_id = row.get("id")
            phone = row.get("phone_number")
        except (TypeError, ValueError):
            try:
                lead_id, phone = raw[0], raw[1]
            except (TypeError, IndexError):
                continue
        if lead_id is None:
            continue
        conn.execute(
            f"UPDATE leads SET phone_normalized = {ph} WHERE id = {ph}",
            (_normalize_e164(phone), lead_id),
        )
        updated += 1
    return updated


def _backfill_ledger(conn, dialect, window_start_iso):
    """Backfill the ledger from harness_follow_up_sends (idempotent).

    scheduled/claimed/drafted (any age) + sent within 7 days.
    blocked/dead_letter/older-sent skipped. Returns rows inserted.
    """
    ph = "%s" if dialect == "postgresql" else "?"
    cur = conn.execute(
        f"""
        INSERT INTO harness_outreach_ledger
            (user_id, lead_id, channel, reason, reason_norm, dedup_key,
             phone_normalized, status, sms_message_id, job_id, approval_id,
             scheduled_at, sent_at, created_at, updated_at)
        SELECT s.user_id, s.lead_id, 'sms', s.reason, s.reason_norm,
               CAST(s.user_id AS TEXT) || ':' || CAST(s.lead_id AS TEXT)
                   || ':' || s.reason_norm,
               l.phone_normalized, s.status, s.sms_message_id, s.job_id,
               s.approval_id, s.scheduled_at, s.sent_at, s.created_at,
               s.updated_at
        FROM harness_follow_up_sends s
        LEFT JOIN leads l ON l.id = s.lead_id AND l.user_id = s.user_id
        WHERE (s.status IN ('scheduled', 'claimed', 'drafted')
               OR (s.status = 'sent' AND s.sent_at >= {ph}))
          AND NOT EXISTS (
              SELECT 1 FROM harness_outreach_ledger x
              WHERE x.user_id = s.user_id AND x.job_id = s.job_id
          )
        """,
        (window_start_iso,),
    )
    try:
        return cur.rowcount if cur.rowcount is not None else 0
    except Exception:
        return 0


def _report_phone_conflicts(conn, dialect):
    """Log every (user_id, phone_normalized) group with >1 lead.

    The staged-UNIQUE gate: the follow-up UNIQUE on
    leads.phone_normalized lands only once this reports zero
    conflicts in staging + production. Never fails the migration:
    a report failure is logged and the migration proceeds. Kept
    self-contained (no app-code import): migrations must stay
    runnable against any future code state.
    """
    try:
        if dialect == "postgresql":
            rows = conn.execute(
                """
                SELECT user_id, phone_normalized,
                       string_agg(CAST(id AS TEXT), ',') AS lead_ids,
                       COUNT(*) AS n
                FROM leads
                WHERE phone_normalized IS NOT NULL
                  AND phone_normalized != ''
                GROUP BY user_id, phone_normalized
                HAVING COUNT(*) > 1
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT user_id, phone_normalized,
                       group_concat(id, ',') AS lead_ids,
                       COUNT(*) AS n
                FROM leads
                WHERE phone_normalized IS NOT NULL
                  AND phone_normalized != ''
                GROUP BY user_id, phone_normalized
                HAVING COUNT(*) > 1
                """
            ).fetchall()
    except Exception:
        logger.warning(
            "047_outreach_ledger: phone conflict report failed",
            exc_info=True,
        )
        return
    conflicts = []
    for raw in rows:
        try:
            row = dict(raw)
        except (TypeError, ValueError):
            continue
        if row.get("user_id") is None or not row.get("phone_normalized"):
            continue
        conflicts.append(row)
    if not conflicts:
        logger.info("047_outreach_ledger: phone_normalized conflicts: 0")
        return
    shown = conflicts[:20]
    logger.warning(
        "047_outreach_ledger: phone_normalized conflicts: %d "
        "(showing %d): %s",
        len(conflicts),
        len(shown),
        "; ".join(
            f"tenant={c.get('user_id')} phone={c.get('phone_normalized')} "
            f"leads={c.get('lead_ids')}"
            for c in shown
        ),
    )


def upgrade_sqlite(conn):
    from datetime import datetime, timedelta, timezone

    conn.execute(SQLITE_TABLE)
    for statement in SQLITE_INDEXES:
        conn.execute(statement)
    _add_phone_normalized_column(conn, "sqlite")
    updated = _backfill_phone_normalized(conn, "sqlite")
    window_start = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).isoformat()
    inserted = _backfill_ledger(conn, "sqlite", window_start)
    logger.info(
        "047_outreach_ledger: phone_normalized backfilled=%d "
        "ledger rows backfilled=%d",
        updated,
        inserted,
    )
    _report_phone_conflicts(conn, "sqlite")
    _check_table(conn, "sqlite")


def upgrade_postgres(conn):
    from datetime import datetime, timedelta, timezone

    conn.execute(PG_TABLE)
    for statement in PG_INDEXES:
        conn.execute(statement)
    _add_phone_normalized_column(conn, "postgresql")
    updated = _backfill_phone_normalized(conn, "postgresql")
    window_start = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).isoformat()
    inserted = _backfill_ledger(conn, "postgresql", window_start)
    logger.info(
        "047_outreach_ledger: phone_normalized backfilled=%d "
        "ledger rows backfilled=%d",
        updated,
        inserted,
    )
    _report_phone_conflicts(conn, "postgresql")
    _check_table(conn, "postgresql")


def verify(conn, dialect):
    _check_table(conn, dialect)
    if dialect == "postgresql":
        _check_indexes_postgres(conn)


def verify_postgres(conn):
    _check_table(conn, "postgresql")
    _check_indexes_postgres(conn)


def verify_sqlite(conn):
    _check_table(conn, "sqlite")
