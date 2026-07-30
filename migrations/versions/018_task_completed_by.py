"""Additive completed_by attribution on tasks for completed-task auditing.

`completed_at` already exists (001_baseline). This adds `completed_by` so the
Tasks feature can show and audit who marked a task complete. Purely additive:
no table drops, no data deletion.
"""

VERSION = "018_task_completed_by"

POSTGRES_COLUMNS = [
    ("tasks", "completed_by", "BIGINT"),
]

SQLITE_COLUMNS = [
    ("tasks", "completed_by", "INTEGER"),
]


def _sqlite_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] if isinstance(r, dict) else r[1] for r in rows}


def _postgres_has_column(conn, table, column):
    raw = conn._raw
    cur = raw.execute(
        """
        SELECT 1 AS ok
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    )
    row = cur.fetchone()
    try:
        cur.close()
    except Exception:
        pass
    return bool(row)


def upgrade_postgres(conn):
    from migrations.pg_ddl import pg_execute

    for table, column, definition in POSTGRES_COLUMNS:
        if not _postgres_has_column(conn, table, column):
            pg_execute(
                conn,
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}",
            )
    pg_execute(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_completed "
        "ON tasks (user_id, status, completed_at)",
    )


def upgrade_sqlite(conn):
    for table, column, definition in SQLITE_COLUMNS:
        cols = _sqlite_columns(conn, table)
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_completed "
        "ON tasks(user_id, status, completed_at)"
    )
