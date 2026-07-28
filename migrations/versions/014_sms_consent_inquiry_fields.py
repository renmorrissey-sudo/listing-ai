"""Additive columns for public SMS consent inquiries (email, name parts, campaign)."""

VERSION = "014_sms_consent_inquiry_fields"

SQLITE_COLS = [
    ("first_name", "TEXT"),
    ("last_name", "TEXT"),
    ("email", "TEXT"),
    ("campaign_source", "TEXT"),
]

PG_COLS = [
    ("first_name", "TEXT"),
    ("last_name", "TEXT"),
    ("email", "TEXT"),
    ("campaign_source", "TEXT"),
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
    for name, typ in SQLITE_COLS:
        if not _sqlite_has_column(conn, "sms_consent_inquiries", name):
            conn.execute(f"ALTER TABLE sms_consent_inquiries ADD COLUMN {name} {typ}")
    # Backfill first/last from name when possible
    try:
        conn.execute(
            """
            UPDATE sms_consent_inquiries
            SET first_name = TRIM(SUBSTR(name, 1, INSTR(name || ' ', ' ') - 1)),
                last_name = TRIM(SUBSTR(name, INSTR(name || ' ', ' ') + 1))
            WHERE (first_name IS NULL OR first_name = '')
              AND name IS NOT NULL AND name != ''
            """
        )
    except Exception:
        pass


def upgrade_postgres(conn):
    for name, typ in PG_COLS:
        if not _postgres_has_column(conn, "sms_consent_inquiries", name):
            conn.execute(f"ALTER TABLE sms_consent_inquiries ADD COLUMN {name} {typ}")
    try:
        conn.execute(
            """
            UPDATE sms_consent_inquiries
            SET first_name = NULLIF(TRIM(SPLIT_PART(name, ' ', 1)), ''),
                last_name = NULLIF(TRIM(SUBSTRING(name FROM POSITION(' ' IN name || ' ') + 1)), '')
            WHERE (first_name IS NULL OR first_name = '')
              AND name IS NOT NULL AND name <> ''
            """
        )
    except Exception:
        pass
