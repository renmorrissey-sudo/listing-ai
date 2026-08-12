"""Tenant-owned email marketing connections and listing email draft exports.

Additive only. SendGrid API keys are encrypted by the application before they
reach ``email_marketing_connections.api_key_encrypted``. Listing exports store
only safe metadata and the provider's Single Send id; generated content remains
in the retained ``listing_generations`` snapshot.
"""

VERSION = "020_listing_email_campaigns"


SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS email_marketing_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        provider TEXT NOT NULL DEFAULT 'sendgrid',
        api_key_encrypted TEXT,
        status TEXT NOT NULL DEFAULT 'connected',
        sender_id INTEGER,
        sender_name TEXT,
        sender_email TEXT,
        default_list_ids_json TEXT,
        suppression_group_id INTEGER,
        suppression_group_name TEXT,
        last_tested_at TEXT,
        last_error_summary TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (user_id, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listing_email_campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        listing_generation_id INTEGER NOT NULL,
        provider TEXT NOT NULL DEFAULT 'sendgrid',
        provider_campaign_id TEXT,
        property_address TEXT NOT NULL,
        subject TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'creating',
        provider_status TEXT,
        error_code TEXT,
        error_summary TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
]


PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS email_marketing_connections (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'sendgrid',
        api_key_encrypted TEXT,
        status TEXT NOT NULL DEFAULT 'connected',
        sender_id BIGINT,
        sender_name TEXT,
        sender_email TEXT,
        default_list_ids_json TEXT,
        suppression_group_id BIGINT,
        suppression_group_name TEXT,
        last_tested_at TIMESTAMPTZ,
        last_error_summary TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (user_id, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listing_email_campaigns (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        listing_generation_id BIGINT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'sendgrid',
        provider_campaign_id TEXT,
        property_address TEXT NOT NULL,
        subject TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'creating',
        provider_status TEXT,
        error_code TEXT,
        error_summary TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
]


INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_email_marketing_connections_user "
    "ON email_marketing_connections(user_id, provider)",
    "CREATE INDEX IF NOT EXISTS idx_listing_email_campaigns_generation "
    "ON listing_email_campaigns(user_id, listing_generation_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_listing_email_campaigns_provider_id "
    "ON listing_email_campaigns(provider, provider_campaign_id)",
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
