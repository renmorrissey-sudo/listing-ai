"""Approved message templates store (049).

Phase III-5: a per-tenant store of agent-approved outbound message
templates. The AI may use an approved template verbatim or with its
named slots filled; it may NOT invent new outbound message templates
or present invented text as approved. Templates carry a name, a
channel (sms / voice), a free-text purpose, a body of at most 480
characters with named slots from a fixed allowlist, and an approval
state (draft / approved / archived).

State machine (enforced server-side in ``harness.templates_db``):
draft -> approved -> archived; any edit to an approved template resets
it to draft; archived -> draft is allowed (rework); archived ->
approved is disallowed. Only status 'approved' templates are ever
usable, and only for their own tenant.

Data safety
------------
Additive and forward-only. One new table + indexes, created
``IF NOT EXISTS`` (Postgres) or after PRAGMA checks (SQLite). No
existing table is altered; no existing data is modified; no seed rows
(the III-4 deterministic texts stay OUT of the store). Postcondition
checks run INLINE in the upgrade functions (the runner does not call
a migration's own ``verify()``) so a failure rolls the migration
back. The Postgres checks only test ``row is not None`` — they never
index into a row, so they are safe against the empty-Postgres test
fake, which returns stub rows. No LIKE in the new SQL (equality
only).

Rollback
--------
Additive. Rollback is a code-only revert — the table stays, unused,
and no data is lost. No down-migration is run (the release has no
down-migrations by design).
"""

import logging

VERSION = "049_message_templates"

TABLE = "harness_message_templates"

COLUMNS = (
    "id",
    "user_id",
    "name",
    "channel",
    "purpose",
    "body",
    "status",
    "created_by",
    "approved_by",
    "approved_at",
    "created_at",
    "updated_at",
)

INDEXES = (
    "idx_msg_tpl_user_status_channel",
    "idx_msg_tpl_user_name",
)

SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS harness_message_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    purpose TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by INTEGER,
    approved_by INTEGER,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, name)
)
"""

PG_TABLE = """
CREATE TABLE IF NOT EXISTS harness_message_templates (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    purpose TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by BIGINT,
    approved_by BIGINT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, name)
)
"""

SQLITE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_msg_tpl_user_status_channel "
    "ON harness_message_templates(user_id, status, channel)",
    "CREATE INDEX IF NOT EXISTS idx_msg_tpl_user_name "
    "ON harness_message_templates(user_id, name)",
)

PG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_msg_tpl_user_status_channel "
    "ON harness_message_templates(user_id, status, channel)",
    "CREATE INDEX IF NOT EXISTS idx_msg_tpl_user_name "
    "ON harness_message_templates(user_id, name)",
)

logger = logging.getLogger(__name__)


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
    ``verify()`` — so, following the 030/045/047 precedent, Postgres
    index verification lives in :func:`verify_postgres`, not in the
    upgrade path. The Postgres checks only test row existence, never
    index into a row.
    """
    if dialect == "postgresql":
        for column in COLUMNS:
            if not _has_column_postgres(conn, TABLE, column):
                raise RuntimeError(
                    "049_message_templates failed: "
                    f"{TABLE}.{column} missing"
                )
        return
    present = _columns_sqlite(conn, TABLE)
    missing = [c for c in COLUMNS if c not in present]
    if missing:
        raise RuntimeError(
            "049_message_templates failed: "
            f"{TABLE} missing columns: {', '.join(missing)}"
        )
    indexes = _indexes_sqlite(conn, TABLE)
    missing_indexes = [i for i in INDEXES if i not in indexes]
    if missing_indexes:
        raise RuntimeError(
            "049_message_templates failed: "
            f"{TABLE} missing indexes: {', '.join(missing_indexes)}"
        )


def _check_indexes_postgres(conn):
    """Postgres index postcondition (verify-time only, see above)."""
    for index in INDEXES:
        if not _has_index_postgres(conn, TABLE, index):
            raise RuntimeError(
                "049_message_templates failed: "
                f"index {index} missing"
            )


def upgrade_sqlite(conn):
    conn.execute(SQLITE_TABLE)
    for statement in SQLITE_INDEXES:
        conn.execute(statement)
    logger.info(
        "049_message_templates: table created (harness_message_templates)"
    )
    _check_table(conn, "sqlite")


def upgrade_postgres(conn):
    conn.execute(PG_TABLE)
    for statement in PG_INDEXES:
        conn.execute(statement)
    logger.info(
        "049_message_templates: table created (harness_message_templates)"
    )
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
