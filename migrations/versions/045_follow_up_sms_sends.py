"""Follow-up SMS send ledger (045).

Phase III-1 gated follow-up sending + scheduling: one row per scheduled
follow-up *send*, created at enqueue time, claimed atomically at fire
time. The durable ``harness_jobs`` row carries the execution lifecycle
(pending -> claimed -> succeeded / dead_letter); this table carries the
send lifecycle (scheduled -> claimed -> sent / drafted / blocked /
deferred / dead_letter) and is the feed for the lead+reason duplicate
guard (``user_id, lead_id, reason_norm``).

Data safety
------------
Additive and forward-only. One new table plus four indexes, created
``IF NOT EXISTS`` (Postgres) or after PRAGMA checks (SQLite). No
existing table is altered, no existing data is modified, no seed rows.
Postcondition checks run INLINE in the upgrade functions (the runner
does not call a migration's own ``verify()``) so a failure rolls the
migration back. The Postgres checks only test ``row is not None`` —
they never index into a row, so they are safe against the
empty-Postgres test fake, which returns stub rows.

Rollback
--------
Additive. Rollback is a code-only revert — the table stays, unused,
and no data is lost. No down-migration is run (the release has no
down-migrations by design).
"""

VERSION = "045_follow_up_sms_sends"

TABLE = "harness_follow_up_sends"

COLUMNS = (
    "id",
    "user_id",
    "lead_id",
    "follow_up_id",
    "job_id",
    "reason",
    "reason_norm",
    "sms_message_id",
    "status",
    "scheduled_at",
    "sent_at",
    "provider_message_id",
    "approval_id",
    "deferred_count",
    "last_gate_reason",
    "created_at",
    "updated_at",
)

INDEXES = (
    "idx_follow_up_sends_user_job",
    "idx_follow_up_sends_due",
    "idx_follow_up_sends_lead_reason",
    "idx_follow_up_sends_lead",
)

SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS harness_follow_up_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    lead_id INTEGER NOT NULL,
    follow_up_id INTEGER,
    job_id INTEGER UNIQUE,
    reason TEXT NOT NULL DEFAULT 'Follow up',
    reason_norm TEXT NOT NULL DEFAULT 'follow up',
    sms_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_at TEXT NOT NULL,
    sent_at TEXT,
    provider_message_id TEXT,
    approval_id INTEGER,
    deferred_count INTEGER NOT NULL DEFAULT 0,
    last_gate_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

PG_TABLE = """
CREATE TABLE IF NOT EXISTS harness_follow_up_sends (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    lead_id BIGINT NOT NULL,
    follow_up_id BIGINT,
    job_id BIGINT UNIQUE,
    reason TEXT NOT NULL DEFAULT 'Follow up',
    reason_norm TEXT NOT NULL DEFAULT 'follow up',
    sms_message_id BIGINT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_at TEXT NOT NULL,
    sent_at TEXT,
    provider_message_id TEXT,
    approval_id BIGINT,
    deferred_count INTEGER NOT NULL DEFAULT 0,
    last_gate_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

SQLITE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_follow_up_sends_user_job "
    "ON harness_follow_up_sends(user_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_follow_up_sends_due "
    "ON harness_follow_up_sends(user_id, status, scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_follow_up_sends_lead_reason "
    "ON harness_follow_up_sends(user_id, lead_id, reason_norm, status)",
    "CREATE INDEX IF NOT EXISTS idx_follow_up_sends_lead "
    "ON harness_follow_up_sends(user_id, lead_id, created_at)",
)

PG_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_follow_up_sends_user_job "
    "ON harness_follow_up_sends(user_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_follow_up_sends_due "
    "ON harness_follow_up_sends(user_id, status, scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_follow_up_sends_lead_reason "
    "ON harness_follow_up_sends(user_id, lead_id, reason_norm, status)",
    "CREATE INDEX IF NOT EXISTS idx_follow_up_sends_lead "
    "ON harness_follow_up_sends(user_id, lead_id, created_at)",
)


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
    empty-Postgres test fake (tests/test_pg_baseline_empty.py) stubs
    ``information_schema.columns`` but not ``pg_indexes``, and the
    runner never calls a migration's own ``verify()`` — so, following
    the 030 precedent, Postgres index verification lives in
    :func:`verify_postgres`, not in the upgrade path. The Postgres
    checks only test row existence, never index into a row.
    """
    if dialect == "postgresql":
        for column in COLUMNS:
            if not _has_column_postgres(conn, TABLE, column):
                raise RuntimeError(
                    "045_follow_up_sms_sends failed: "
                    f"{TABLE}.{column} missing"
                )
        return
    present = _columns_sqlite(conn, TABLE)
    missing = [c for c in COLUMNS if c not in present]
    if missing:
        raise RuntimeError(
            "045_follow_up_sms_sends failed: "
            f"{TABLE} missing columns: {', '.join(missing)}"
        )
    indexes = _indexes_sqlite(conn, TABLE)
    missing_indexes = [i for i in INDEXES if i not in indexes]
    if missing_indexes:
        raise RuntimeError(
            "045_follow_up_sms_sends failed: "
            f"{TABLE} missing indexes: {', '.join(missing_indexes)}"
        )


def _check_indexes_postgres(conn):
    """Postgres index postcondition (verify-time only, see above)."""
    for index in INDEXES:
        if not _has_index_postgres(conn, TABLE, index):
            raise RuntimeError(
                "045_follow_up_sms_sends failed: "
                f"index {index} missing"
            )


def upgrade_sqlite(conn):
    conn.execute(SQLITE_TABLE)
    for statement in SQLITE_INDEXES:
        conn.execute(statement)
    _check_table(conn, "sqlite")


def upgrade_postgres(conn):
    conn.execute(PG_TABLE)
    for statement in PG_INDEXES:
        conn.execute(statement)
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
