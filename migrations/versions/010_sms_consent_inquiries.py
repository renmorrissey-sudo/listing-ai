"""Create public SMS consent inquiry table (additive; never drops data)."""

VERSION = "010_sms_consent_inquiries"

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS sms_consent_inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    message TEXT NOT NULL,
    sms_consent INTEGER NOT NULL DEFAULT 0,
    consent_at TEXT,
    source_url TEXT,
    disclosure_version TEXT,
    ip_address TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL
)
"""

POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS sms_consent_inquiries (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    message TEXT NOT NULL,
    sms_consent BOOLEAN NOT NULL DEFAULT FALSE,
    consent_at TIMESTAMPTZ,
    source_url TEXT,
    disclosure_version TEXT,
    ip_address TEXT,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _postgres_table_exists(conn, table):
    from migrations.pg_ddl import pg_table_exists

    return pg_table_exists(conn, table)


def upgrade_sqlite(conn):
    conn.execute(SQLITE_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_consent_inquiries_phone "
        "ON sms_consent_inquiries(phone_number)"
    )


def upgrade_postgres(conn):
    from migrations.pg_ddl import pg_execute

    if not _postgres_table_exists(conn, "sms_consent_inquiries"):
        pg_execute(conn, POSTGRES_DDL)
    pg_execute(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_sms_consent_inquiries_phone "
        "ON sms_consent_inquiries(phone_number)",
    )
