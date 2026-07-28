"""Additive external lead sources, SMS consent evidence, pond fields, and safe backfill."""

VERSION = "011_external_leads_consent"

LEAD_COLUMNS_SQLITE = [
    ("sms_consent_status", "TEXT NOT NULL DEFAULT 'unverified'"),
    ("sms_sending_blocked", "INTEGER NOT NULL DEFAULT 1"),
    ("external_source_id", "INTEGER"),
    ("external_record_id", "TEXT"),
    ("external_payload_meta", "TEXT"),
    ("email", "TEXT"),
    ("pond_status", "TEXT NOT NULL DEFAULT 'assigned'"),
    ("claimed_at", "TEXT"),
    ("claimed_by_user_id", "INTEGER"),
    ("import_batch_id", "INTEGER"),
]

LEAD_COLUMNS_PG = [
    ("sms_consent_status", "TEXT NOT NULL DEFAULT 'unverified'"),
    ("sms_sending_blocked", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("external_source_id", "BIGINT"),
    ("external_record_id", "TEXT"),
    ("external_payload_meta", "TEXT"),
    ("email", "TEXT"),
    ("pond_status", "TEXT NOT NULL DEFAULT 'assigned'"),
    ("claimed_at", "TIMESTAMPTZ"),
    ("claimed_by_user_id", "BIGINT"),
    ("import_batch_id", "BIGINT"),
]

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS external_lead_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other',
        provider_key TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        import_method TEXT NOT NULL DEFAULT 'manual',
        consent_behavior TEXT NOT NULL DEFAULT 'unverified_blocked',
        default_lead_type TEXT,
        default_lead_status TEXT DEFAULT 'new',
        default_pond_status TEXT DEFAULT 'claimable',
        webhook_secret_hash TEXT,
        metadata_mapping_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (user_id, provider_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS external_lead_import_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        external_source_id INTEGER,
        filename TEXT,
        created_count INTEGER NOT NULL DEFAULT 0,
        updated_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        invalid_count INTEGER NOT NULL DEFAULT 0,
        pending_evidence_count INTEGER NOT NULL DEFAULT 0,
        error_summary TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_consent_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL,
        consent_status TEXT NOT NULL DEFAULT 'pending',
        consent_method TEXT,
        source_provider TEXT,
        source_record_id TEXT,
        source_url TEXT,
        consent_at TEXT,
        recorded_at TEXT NOT NULL,
        authorized_agent_name TEXT,
        authorized_brokerage_name TEXT,
        phone_number TEXT,
        communication_purpose TEXT,
        disclosure_text TEXT,
        disclosure_version TEXT,
        evidence_type TEXT,
        upload_ref TEXT,
        notes TEXT,
        confirmed_by_user_id INTEGER,
        confirmed_at TEXT,
        revoked_at TEXT,
        attestation_accepted INTEGER NOT NULL DEFAULT 0,
        audit_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consent_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        lead_id INTEGER,
        actor_user_id INTEGER,
        action TEXT NOT NULL,
        previous_value TEXT,
        new_value TEXT,
        source TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
]

POSTGRES_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS external_lead_sources (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other',
        provider_key TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        import_method TEXT NOT NULL DEFAULT 'manual',
        consent_behavior TEXT NOT NULL DEFAULT 'unverified_blocked',
        default_lead_type TEXT,
        default_lead_status TEXT DEFAULT 'new',
        default_pond_status TEXT DEFAULT 'claimable',
        webhook_secret_hash TEXT,
        metadata_mapping_json TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (user_id, provider_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS external_lead_import_batches (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        external_source_id BIGINT,
        filename TEXT,
        created_count INTEGER NOT NULL DEFAULT 0,
        updated_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        invalid_count INTEGER NOT NULL DEFAULT 0,
        pending_evidence_count INTEGER NOT NULL DEFAULT 0,
        error_summary TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_consent_evidence (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        lead_id BIGINT NOT NULL,
        consent_status TEXT NOT NULL DEFAULT 'pending',
        consent_method TEXT,
        source_provider TEXT,
        source_record_id TEXT,
        source_url TEXT,
        consent_at TIMESTAMPTZ,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        authorized_agent_name TEXT,
        authorized_brokerage_name TEXT,
        phone_number TEXT,
        communication_purpose TEXT,
        disclosure_text TEXT,
        disclosure_version TEXT,
        evidence_type TEXT,
        upload_ref TEXT,
        notes TEXT,
        confirmed_by_user_id BIGINT,
        confirmed_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        attestation_accepted BOOLEAN NOT NULL DEFAULT FALSE,
        audit_json TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consent_audit_events (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        lead_id BIGINT,
        actor_user_id BIGINT,
        action TEXT NOT NULL,
        previous_value TEXT,
        new_value TEXT,
        source TEXT,
        metadata_json TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]


def _sqlite_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] if isinstance(r, dict) else r[1] for r in rows}


def _postgres_has_column(conn, table, column):
    from migrations.pg_ddl import pg_execute

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


def _backfill_sqlite(conn):
    cols = _sqlite_columns(conn, "leads")
    if "sms_consent_status" not in cols:
        return
    conn.execute(
        """
        UPDATE leads
        SET sms_consent_status = 'opted_out', sms_sending_blocked = 1
        WHERE opt_out_status = 'opted_out'
        """
    )
    conn.execute(
        """
        UPDATE leads
        SET sms_consent_status = 'verified', sms_sending_blocked = 0
        WHERE IFNULL(opt_out_status, 'active') != 'opted_out'
          AND consent_status = 'confirmed'
          AND (sms_consent_status IS NULL OR sms_consent_status = 'unverified')
        """
    )


def _backfill_postgres(conn):
    from migrations.pg_ddl import pg_execute

    if not _postgres_has_column(conn, "leads", "sms_consent_status"):
        return
    pg_execute(
        conn,
        """
        UPDATE leads
        SET sms_consent_status = 'opted_out', sms_sending_blocked = TRUE
        WHERE opt_out_status = 'opted_out'
        """,
    )
    pg_execute(
        conn,
        """
        UPDATE leads
        SET sms_consent_status = 'verified', sms_sending_blocked = FALSE
        WHERE COALESCE(opt_out_status, 'active') != 'opted_out'
          AND consent_status = 'confirmed'
          AND (sms_consent_status IS NULL OR sms_consent_status = 'unverified')
        """,
    )


def upgrade_sqlite(conn):
    cols = _sqlite_columns(conn, "leads")
    for column, definition in LEAD_COLUMNS_SQLITE:
        if column not in cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {definition}")
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leads_external_record "
        "ON leads(user_id, external_source_id, external_record_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leads_sms_consent "
        "ON leads(user_id, sms_consent_status, sms_sending_blocked)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_consent_evidence_lead "
        "ON sms_consent_evidence(user_id, lead_id)"
    )
    _backfill_sqlite(conn)


def upgrade_postgres(conn):
    from migrations.pg_ddl import pg_execute

    for column, definition in LEAD_COLUMNS_PG:
        if not _postgres_has_column(conn, "leads", column):
            pg_execute(
                conn,
                f"ALTER TABLE leads ADD COLUMN IF NOT EXISTS {column} {definition}",
            )
    for ddl in POSTGRES_TABLES:
        pg_execute(conn, ddl)
    pg_execute(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_leads_external_record "
        "ON leads(user_id, external_source_id, external_record_id)",
    )
    pg_execute(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_leads_sms_consent "
        "ON leads(user_id, sms_consent_status, sms_sending_blocked)",
    )
    pg_execute(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_consent_evidence_lead "
        "ON sms_consent_evidence(user_id, lead_id)",
    )
    _backfill_postgres(conn)
