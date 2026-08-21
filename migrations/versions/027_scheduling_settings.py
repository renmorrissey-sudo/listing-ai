"""Account-level scheduling preferences for autonomous appointment booking."""

VERSION = "027_scheduling_settings"

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS user_scheduling_settings (
        user_id INTEGER PRIMARY KEY,
        default_duration_minutes INTEGER NOT NULL DEFAULT 30,
        business_hours_start TEXT NOT NULL DEFAULT '08:00',
        business_hours_end TEXT NOT NULL DEFAULT '18:00',
        business_days TEXT NOT NULL DEFAULT '0,1,2,3,4',
        min_notice_minutes INTEGER NOT NULL DEFAULT 60,
        buffer_minutes INTEGER NOT NULL DEFAULT 15,
        updated_at TEXT
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS user_scheduling_settings (
        user_id BIGINT PRIMARY KEY,
        default_duration_minutes INTEGER NOT NULL DEFAULT 30,
        business_hours_start TEXT NOT NULL DEFAULT '08:00',
        business_hours_end TEXT NOT NULL DEFAULT '18:00',
        business_days TEXT NOT NULL DEFAULT '0,1,2,3,4',
        min_notice_minutes INTEGER NOT NULL DEFAULT 60,
        buffer_minutes INTEGER NOT NULL DEFAULT 15,
        updated_at TEXT
    )
    """,
]


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
