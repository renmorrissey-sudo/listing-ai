"""Ask TopAI live conversation tables.

Additive only. ask_topai_tool_invocations stores idempotent realtime tool
results so reconnects and model retries cannot duplicate CRM writes.
"""

VERSION = "026_ask_topai_live"

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS ask_topai_tool_invocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        session_key TEXT NOT NULL,
        call_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments_json TEXT,
        result_json TEXT,
        status TEXT NOT NULL,
        lead_id INTEGER,
        created_at TEXT NOT NULL
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS ask_topai_tool_invocations (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        session_key TEXT NOT NULL,
        call_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments_json TEXT,
        result_json TEXT,
        status TEXT NOT NULL,
        lead_id BIGINT,
        created_at TEXT NOT NULL
    )
    """,
]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_topai_tool_call "
    "ON ask_topai_tool_invocations(user_id, session_key, call_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_topai_tool_action "
    "ON ask_topai_tool_invocations(user_id, session_key, action_id)",
    "CREATE INDEX IF NOT EXISTS idx_ask_topai_tool_session "
    "ON ask_topai_tool_invocations(user_id, session_key, created_at)",
]


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)
