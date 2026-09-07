"""Add tenant-scoped Competitive Market Analysis reports."""

VERSION = "026_cma_reports"

SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS cma_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    prospect_type TEXT NOT NULL,
    prospect_name TEXT,
    subject_address TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    comp_count INTEGER NOT NULL,
    date_range_months INTEGER NOT NULL,
    criteria_json TEXT NOT NULL,
    comparables_json TEXT NOT NULL,
    excluded_json TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

POSTGRES_TABLE = """
CREATE TABLE IF NOT EXISTS cma_reports (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    prospect_type TEXT NOT NULL,
    prospect_name TEXT,
    subject_address TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    comp_count INTEGER NOT NULL,
    date_range_months INTEGER NOT NULL,
    criteria_json TEXT NOT NULL,
    comparables_json TEXT NOT NULL,
    excluded_json TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_cma_reports_user_created ON cma_reports(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cma_reports_user_location ON cma_reports(user_id, city, state)",
)


def upgrade_sqlite(conn):
    conn.execute(SQLITE_TABLE)
    for sql in INDEXES:
        conn.execute(sql)


def upgrade_postgres(conn):
    conn.execute(POSTGRES_TABLE)
    for sql in INDEXES:
        conn.execute(sql)
