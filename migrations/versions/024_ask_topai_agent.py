"""Ask TopAI conversation sessions + extra audit columns.

Additive only. ask_topai_sessions holds short-lived clarification history.
ask_topai_commands gains model, input_source, tools_invoked_json, session_key.
"""

VERSION = "024_ask_topai_agent"

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS ask_topai_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        session_key TEXT NOT NULL,
        messages_json TEXT,
        pending_json TEXT,
        status TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS ask_topai_sessions (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        session_key TEXT NOT NULL,
        messages_json TEXT,
        pending_json TEXT,
        status TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_topai_sessions_user_key ON ask_topai_sessions(user_id, session_key)",
    "CREATE INDEX IF NOT EXISTS idx_ask_topai_sessions_user ON ask_topai_sessions(user_id, updated_at)",
]

COMMAND_COLS = (
    ("model", "TEXT"),
    ("input_source", "TEXT"),
    ("tools_invoked_json", "TEXT"),
    ("session_key", "TEXT"),
)


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
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)
    for column, coltype in COMMAND_COLS:
        if not _sqlite_has_column(conn, "ask_topai_commands", column):
            conn.execute(f"ALTER TABLE ask_topai_commands ADD COLUMN {column} {coltype}")


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)
    for column, coltype in COMMAND_COLS:
        if not _postgres_has_column(conn, "ask_topai_commands", column):
            conn.execute(f"ALTER TABLE ask_topai_commands ADD COLUMN {column} {coltype}")
