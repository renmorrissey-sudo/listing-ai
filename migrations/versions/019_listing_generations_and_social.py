"""Persistent Listing Generator history + social media connections/publishing.

Additive only. New tables:
- listing_generations: one row per successful Listing Generator run. Freezes the
  exact generated output so reopening a past generation never re-derives
  different content. expires_at = created_at + 60 days; enforced both by
  application queries (WHERE expires_at > now) and by a periodic cleanup job.
- social_connections: one row per tenant-connected social account (OAuth
  tokens stored encrypted at the application layer — this migration only
  defines the column, encryption happens in social_tokens.py).
- social_oauth_states: short-lived CSRF state tokens for the OAuth
  authorize/callback round trip.
- social_publications: one row per publish attempt of a listing_generation to
  a specific social_connection. idempotency_key has a unique index so retries
  (double-click, HTTP retry, webhook retry) cannot create duplicate posts.
"""

VERSION = "019_listing_generations_and_social"

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS listing_generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        display_address TEXT NOT NULL,
        normalized_address TEXT NOT NULL,
        input_snapshot_json TEXT,
        output_snapshot_json TEXT NOT NULL,
        social_content_json TEXT,
        status TEXT NOT NULL DEFAULT 'completed',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS social_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        external_account_id TEXT,
        display_name TEXT,
        access_token_encrypted TEXT,
        refresh_token_encrypted TEXT,
        token_expires_at TEXT,
        scopes TEXT,
        status TEXT NOT NULL DEFAULT 'connected',
        default_enabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (user_id, provider, external_account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS social_oauth_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        state TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        code_verifier TEXT,
        redirect_uri TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS social_publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        listing_generation_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        social_connection_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        provider_post_id TEXT,
        provider_post_url TEXT,
        error_code TEXT,
        error_summary TEXT,
        idempotency_key TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        published_at TEXT,
        UNIQUE (idempotency_key)
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS listing_generations (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        display_address TEXT NOT NULL,
        normalized_address TEXT NOT NULL,
        input_snapshot_json TEXT,
        output_snapshot_json TEXT NOT NULL,
        social_content_json TEXT,
        status TEXT NOT NULL DEFAULT 'completed',
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS social_connections (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        provider TEXT NOT NULL,
        external_account_id TEXT,
        display_name TEXT,
        access_token_encrypted TEXT,
        refresh_token_encrypted TEXT,
        token_expires_at TIMESTAMPTZ,
        scopes TEXT,
        status TEXT NOT NULL DEFAULT 'connected',
        default_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (user_id, provider, external_account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS social_oauth_states (
        id BIGSERIAL PRIMARY KEY,
        state TEXT NOT NULL UNIQUE,
        user_id BIGINT NOT NULL,
        provider TEXT NOT NULL,
        code_verifier TEXT,
        redirect_uri TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS social_publications (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        listing_generation_id BIGINT NOT NULL,
        provider TEXT NOT NULL,
        social_connection_id BIGINT,
        status TEXT NOT NULL DEFAULT 'pending',
        provider_post_id TEXT,
        provider_post_url TEXT,
        error_code TEXT,
        error_summary TEXT,
        idempotency_key TEXT NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL,
        published_at TIMESTAMPTZ,
        UNIQUE (idempotency_key)
    )
    """,
]

SQLITE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_listing_generations_user_created "
    "ON listing_generations(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_listing_generations_user_address "
    "ON listing_generations(user_id, normalized_address, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_listing_generations_expires "
    "ON listing_generations(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_social_connections_user "
    "ON social_connections(user_id, provider)",
    "CREATE INDEX IF NOT EXISTS idx_social_oauth_states_expires "
    "ON social_oauth_states(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_social_publications_generation "
    "ON social_publications(listing_generation_id)",
    "CREATE INDEX IF NOT EXISTS idx_social_publications_user "
    "ON social_publications(user_id)",
]

PG_INDEXES = SQLITE_INDEXES


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    for sql in SQLITE_INDEXES:
        conn.execute(sql)


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    for sql in PG_INDEXES:
        conn.execute(sql)
