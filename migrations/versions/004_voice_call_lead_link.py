"""Link voice_calls to CRM leads; add call contact timestamps on leads."""

VERSION = "004_voice_call_lead_link"

POSTGRES_COLUMNS = [
    ("voice_calls", "lead_id", "BIGINT"),
    ("leads", "last_contacted_at", "TEXT"),
    ("leads", "latest_call_at", "TEXT"),
]

SQLITE_COLUMNS = [
    ("voice_calls", "lead_id", "INTEGER"),
    ("leads", "last_contacted_at", "TEXT"),
    ("leads", "latest_call_at", "TEXT"),
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


def _postgres_constraint_exists(conn, name):
    raw = conn._raw
    cur = raw.execute(
        """
        SELECT 1 AS ok
        FROM information_schema.table_constraints
        WHERE table_schema = 'public' AND constraint_name = %s
        LIMIT 1
        """,
        (name,),
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
        "CREATE INDEX IF NOT EXISTS idx_voice_calls_lead_id ON voice_calls (lead_id)",
    )
    pg_execute(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_voice_calls_user_phone ON voice_calls (user_id, phone_number)",
    )

    # Proper FK without cascade-delete of call history when a lead is removed.
    if not _postgres_constraint_exists(conn, "fk_voice_calls_lead_id"):
        pg_execute(
            conn,
            """
            ALTER TABLE voice_calls
            ADD CONSTRAINT fk_voice_calls_lead_id
            FOREIGN KEY (lead_id) REFERENCES leads (id)
            ON DELETE SET NULL
            """,
        )


def upgrade_sqlite(conn):
    for table, column, definition in SQLITE_COLUMNS:
        cols = _sqlite_columns(conn, table)
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_calls_lead_id ON voice_calls (lead_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_calls_user_phone ON voice_calls (user_id, phone_number)"
    )
