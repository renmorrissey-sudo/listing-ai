"""Add source spend tracking and per-lead gross commission income."""

VERSION = "025_external_lead_source_roi"


def _sqlite_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] if isinstance(row, dict) else row[1] for row in rows}


def _postgres_has_column(conn, table, column):
    cur = conn._raw.execute(
        """
        SELECT 1
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


def upgrade_sqlite(conn):
    if "gross_commission_income_cents" not in _sqlite_columns(conn, "leads"):
        conn.execute(
            "ALTER TABLE leads ADD COLUMN gross_commission_income_cents INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_lead_source_spend (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            external_source_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            spent_on TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_source_spend_user_source_date "
        "ON external_lead_source_spend(user_id, external_source_id, spent_on)"
    )


def upgrade_postgres(conn):
    from migrations.pg_ddl import pg_execute

    if not _postgres_has_column(conn, "leads", "gross_commission_income_cents"):
        pg_execute(
            conn,
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS "
            "gross_commission_income_cents BIGINT NOT NULL DEFAULT 0",
        )
    pg_execute(
        conn,
        """
        CREATE TABLE IF NOT EXISTS external_lead_source_spend (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            external_source_id BIGINT NOT NULL,
            amount_cents BIGINT NOT NULL,
            spent_on DATE NOT NULL,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    )
    pg_execute(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_external_source_spend_user_source_date "
        "ON external_lead_source_spend(user_id, external_source_id, spent_on)",
    )
