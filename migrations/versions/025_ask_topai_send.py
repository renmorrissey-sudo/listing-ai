"""Ask TopAI Send idempotency key on command audit rows.

Additive only. request_id_hash lets a repeated Send reuse the prior result
instead of duplicating leads, notes, tasks, or criteria updates.
"""

VERSION = "025_ask_topai_send"

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_topai_commands_request "
    "ON ask_topai_commands(user_id, request_id_hash)",
]


def _sqlite_has_column(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {dict(r)["name"] if hasattr(r, "keys") else r[1] for r in rows}
    return column in names


def _postgres_has_column(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return bool(row)


def upgrade_sqlite(conn):
    if not _sqlite_has_column(conn, "ask_topai_commands", "request_id_hash"):
        conn.execute("ALTER TABLE ask_topai_commands ADD COLUMN request_id_hash TEXT")
    for sql in INDEXES:
        conn.execute(sql)


def upgrade_postgres(conn):
    if not _postgres_has_column(conn, "ask_topai_commands", "request_id_hash"):
        conn.execute("ALTER TABLE ask_topai_commands ADD COLUMN request_id_hash TEXT")
    for sql in INDEXES:
        conn.execute(sql)
