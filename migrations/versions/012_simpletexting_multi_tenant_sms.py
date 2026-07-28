"""Additive SimpleTexting multi-tenant senders, attestations, campaigns, and jobs."""

VERSION = "012_simpletexting_multi_tenant_sms"

LEAD_STATUS_BACKFILL_SQLITE = """
UPDATE leads SET sms_consent_status = CASE
  WHEN opt_out_status = 'opted_out' OR sms_consent_status = 'opted_out' THEN 'opted_out'
  WHEN sms_consent_status = 'verified' THEN 'user_certified'
  WHEN sms_consent_status = 'revoked' THEN 'revoked'
  WHEN sms_consent_status = 'not_permitted' THEN 'suppressed'
  WHEN sms_consent_status IN ('not_certified', 'user_certified', 'suppressed', 'invalid_number') THEN sms_consent_status
  ELSE 'not_certified'
END
WHERE sms_consent_status IS NOT NULL OR opt_out_status IS NOT NULL
"""

LEAD_STATUS_BACKFILL_PG = LEAD_STATUS_BACKFILL_SQLITE

SMS_MESSAGE_COLS_SQLITE = [
    ("campaign_id", "INTEGER"),
    ("from_number", "TEXT"),
    ("segments", "INTEGER"),
    ("credits", "INTEGER"),
    ("attestation_id", "INTEGER"),
    ("idempotency_key", "TEXT"),
]

SMS_MESSAGE_COLS_PG = [
    ("campaign_id", "BIGINT"),
    ("from_number", "TEXT"),
    ("segments", "INTEGER"),
    ("credits", "INTEGER"),
    ("attestation_id", "BIGINT"),
    ("idempotency_key", "TEXT"),
]

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS tenant_sms_senders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        sms_provider TEXT NOT NULL DEFAULT 'simpletexting',
        sender_number TEXT NOT NULL,
        provider_number_id TEXT,
        provider_account_reference TEXT,
        sms_enabled INTEGER NOT NULL DEFAULT 0,
        registration_status TEXT NOT NULL DEFAULT 'pending',
        activated_at TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (sender_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_subscriber_attestations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        actor_user_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL,
        campaign_id INTEGER,
        message_purpose TEXT NOT NULL,
        message_hash TEXT NOT NULL,
        certification_text_version TEXT NOT NULL,
        source_page TEXT,
        provider TEXT,
        certified_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaign_attestations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        actor_user_id INTEGER NOT NULL,
        campaign_id INTEGER NOT NULL,
        eligible_count INTEGER NOT NULL DEFAULT 0,
        excluded_count INTEGER NOT NULL DEFAULT 0,
        campaign_purpose TEXT NOT NULL,
        message_hash TEXT NOT NULL,
        audience_snapshot_id TEXT NOT NULL,
        certification_text_version TEXT NOT NULL,
        provider TEXT,
        scheduled_launch_at TEXT,
        certified_at TEXT NOT NULL,
        invalidated_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_suppression_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phone_number TEXT NOT NULL,
        reason TEXT NOT NULL,
        source TEXT,
        lead_id INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE (user_id, phone_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        message_template TEXT,
        merge_defaults_json TEXT,
        campaign_purpose TEXT,
        sender_number TEXT,
        scheduled_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        content_fingerprint TEXT,
        audience_snapshot_id TEXT,
        attestation_id INTEGER,
        test_mode INTEGER NOT NULL DEFAULT 0,
        limits_json TEXT,
        stats_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaign_recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        lead_id INTEGER,
        phone_number TEXT NOT NULL,
        merge_fields_json TEXT,
        exclusion_reason TEXT,
        eligible INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaign_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        recipient_id INTEGER NOT NULL,
        lead_id INTEGER,
        phone_number TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        idempotency_key TEXT NOT NULL UNIQUE,
        claimed_at TEXT,
        claimed_by TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        provider_message_id TEXT,
        sms_message_id INTEGER,
        failure_code TEXT,
        failure_message TEXT,
        submitted_at TEXT,
        delivered_at TEXT,
        failed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_link_clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        campaign_id INTEGER,
        lead_id INTEGER,
        tracking_token TEXT NOT NULL UNIQUE,
        destination_url TEXT NOT NULL,
        first_clicked_at TEXT,
        latest_clicked_at TEXT,
        total_clicks INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_terms_acceptances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        actor_user_id INTEGER NOT NULL,
        terms_version TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        accepted_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        actor_user_id INTEGER,
        campaign_id INTEGER,
        lead_id INTEGER,
        action TEXT NOT NULL,
        previous_value TEXT,
        new_value TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS tenant_sms_senders (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        sms_provider TEXT NOT NULL DEFAULT 'simpletexting',
        sender_number TEXT NOT NULL,
        provider_number_id TEXT,
        provider_account_reference TEXT,
        sms_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        registration_status TEXT NOT NULL DEFAULT 'pending',
        activated_at TIMESTAMPTZ,
        metadata_json TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (sender_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_subscriber_attestations (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        actor_user_id BIGINT NOT NULL,
        lead_id BIGINT NOT NULL,
        campaign_id BIGINT,
        message_purpose TEXT NOT NULL,
        message_hash TEXT NOT NULL,
        certification_text_version TEXT NOT NULL,
        source_page TEXT,
        provider TEXT,
        certified_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaign_attestations (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        actor_user_id BIGINT NOT NULL,
        campaign_id BIGINT NOT NULL,
        eligible_count INTEGER NOT NULL DEFAULT 0,
        excluded_count INTEGER NOT NULL DEFAULT 0,
        campaign_purpose TEXT NOT NULL,
        message_hash TEXT NOT NULL,
        audience_snapshot_id TEXT NOT NULL,
        certification_text_version TEXT NOT NULL,
        provider TEXT,
        scheduled_launch_at TIMESTAMPTZ,
        certified_at TIMESTAMPTZ NOT NULL,
        invalidated_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_suppression_list (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        phone_number TEXT NOT NULL,
        reason TEXT NOT NULL,
        source TEXT,
        lead_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (user_id, phone_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaigns (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        message_template TEXT,
        merge_defaults_json TEXT,
        campaign_purpose TEXT,
        sender_number TEXT,
        scheduled_at TIMESTAMPTZ,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        content_fingerprint TEXT,
        audience_snapshot_id TEXT,
        attestation_id BIGINT,
        test_mode BOOLEAN NOT NULL DEFAULT FALSE,
        limits_json TEXT,
        stats_json TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaign_recipients (
        id BIGSERIAL PRIMARY KEY,
        campaign_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        lead_id BIGINT,
        phone_number TEXT NOT NULL,
        merge_fields_json TEXT,
        exclusion_reason TEXT,
        eligible BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_campaign_jobs (
        id BIGSERIAL PRIMARY KEY,
        campaign_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        recipient_id BIGINT NOT NULL,
        lead_id BIGINT,
        phone_number TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        idempotency_key TEXT NOT NULL UNIQUE,
        claimed_at TIMESTAMPTZ,
        claimed_by TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ,
        provider_message_id TEXT,
        sms_message_id BIGINT,
        failure_code TEXT,
        failure_message TEXT,
        submitted_at TIMESTAMPTZ,
        delivered_at TIMESTAMPTZ,
        failed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_link_clicks (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        campaign_id BIGINT,
        lead_id BIGINT,
        tracking_token TEXT NOT NULL UNIQUE,
        destination_url TEXT NOT NULL,
        first_clicked_at TIMESTAMPTZ,
        latest_clicked_at TIMESTAMPTZ,
        total_clicks INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_terms_acceptances (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        actor_user_id BIGINT NOT NULL,
        terms_version TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        accepted_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_audit_events (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        actor_user_id BIGINT,
        campaign_id BIGINT,
        lead_id BIGINT,
        action TEXT NOT NULL,
        previous_value TEXT,
        new_value TEXT,
        metadata_json TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
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


def _add_columns(conn, table, columns, *, is_postgres):
    for name, typ in columns:
        has = _postgres_has_column(conn, table, name) if is_postgres else _sqlite_has_column(conn, table, name)
        if has:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    _add_columns(conn, "sms_messages", SMS_MESSAGE_COLS_SQLITE, is_postgres=False)
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_tenant_sms_senders_user ON tenant_sms_senders(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_tenant_sms_senders_number ON tenant_sms_senders(sender_number)",
        "CREATE INDEX IF NOT EXISTS idx_sms_suppression_user_phone ON sms_suppression_list(user_id, phone_number)",
        "CREATE INDEX IF NOT EXISTS idx_sms_campaign_jobs_pending ON sms_campaign_jobs(status, next_attempt_at)",
        "CREATE INDEX IF NOT EXISTS idx_sms_campaign_jobs_campaign ON sms_campaign_jobs(campaign_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_sms_campaign_recipients_campaign ON sms_campaign_recipients(campaign_id)",
        "CREATE INDEX IF NOT EXISTS idx_sms_attestations_lead ON sms_subscriber_attestations(user_id, lead_id)",
        "CREATE INDEX IF NOT EXISTS idx_sms_link_token ON sms_link_clicks(tracking_token)",
    ]:
        conn.execute(sql)
    try:
        conn.execute(LEAD_STATUS_BACKFILL_SQLITE)
    except Exception:
        pass


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    _add_columns(conn, "sms_messages", SMS_MESSAGE_COLS_PG, is_postgres=True)
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_tenant_sms_senders_user ON tenant_sms_senders(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_tenant_sms_senders_number ON tenant_sms_senders(sender_number)",
        "CREATE INDEX IF NOT EXISTS idx_sms_suppression_user_phone ON sms_suppression_list(user_id, phone_number)",
        "CREATE INDEX IF NOT EXISTS idx_sms_campaign_jobs_pending ON sms_campaign_jobs(status, next_attempt_at)",
        "CREATE INDEX IF NOT EXISTS idx_sms_campaign_jobs_campaign ON sms_campaign_jobs(campaign_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_sms_campaign_recipients_campaign ON sms_campaign_recipients(campaign_id)",
        "CREATE INDEX IF NOT EXISTS idx_sms_attestations_lead ON sms_subscriber_attestations(user_id, lead_id)",
        "CREATE INDEX IF NOT EXISTS idx_sms_link_token ON sms_link_clicks(tracking_token)",
    ]:
        conn.execute(sql)
    try:
        conn.execute(LEAD_STATUS_BACKFILL_PG)
    except Exception:
        pass
