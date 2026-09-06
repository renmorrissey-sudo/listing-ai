"""Persist tenant-scoped Call Script runs and Target Area overviews."""

VERSION = "023_call_script_history"

SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS call_script_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    target_area TEXT NOT NULL,
    normalized_target_area TEXT NOT NULL,
    property_type TEXT,
    situation TEXT,
    agent_name TEXT,
    key_benefit TEXT,
    area_overview TEXT,
    opening_script TEXT NOT NULL,
    objection_handlers TEXT NOT NULL,
    voicemail_script TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

POSTGRES_TABLE = """
CREATE TABLE IF NOT EXISTS call_script_generations (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    target_area TEXT NOT NULL,
    normalized_target_area TEXT NOT NULL,
    property_type TEXT,
    situation TEXT,
    agent_name TEXT,
    key_benefit TEXT,
    area_overview TEXT,
    opening_script TEXT NOT NULL,
    objection_handlers TEXT NOT NULL,
    voicemail_script TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_call_scripts_user_created "
    "ON call_script_generations(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_call_scripts_user_area "
    "ON call_script_generations(user_id, normalized_target_area, created_at)",
)


def upgrade_sqlite(conn):
    conn.execute(SQLITE_TABLE)
    for sql in INDEXES:
        conn.execute(sql)


def upgrade_postgres(conn):
    conn.execute(POSTGRES_TABLE)
    for sql in INDEXES:
        conn.execute(sql)
