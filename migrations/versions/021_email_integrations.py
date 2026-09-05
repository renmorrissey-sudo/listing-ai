"""Generic tenant-owned email integrations and OAuth state.

This migration leaves platform transactional SENDGRID_API_KEY configuration
untouched. Existing per-user SendGrid Marketing rows are copied into the new
generic model without decrypting their Fernet ciphertext.
"""

from __future__ import annotations

import json

VERSION = "021_email_integrations"

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS email_integrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        integration_kind TEXT NOT NULL,
        auth_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'connected',
        credential_encrypted TEXT,
        access_token_encrypted TEXT,
        refresh_token_encrypted TEXT,
        token_expires_at TEXT,
        scopes TEXT,
        external_account_id TEXT,
        external_account_email TEXT,
        display_name TEXT,
        sender_id TEXT,
        sender_name TEXT,
        sender_email TEXT,
        settings_json TEXT,
        provider_metadata_json TEXT,
        is_default INTEGER NOT NULL DEFAULT 0,
        last_tested_at TEXT,
        last_error_summary TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (user_id, provider, external_account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_oauth_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        state TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        code_verifier TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS email_integrations (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        provider TEXT NOT NULL,
        integration_kind TEXT NOT NULL,
        auth_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'connected',
        credential_encrypted TEXT,
        access_token_encrypted TEXT,
        refresh_token_encrypted TEXT,
        token_expires_at TIMESTAMPTZ,
        scopes TEXT,
        external_account_id TEXT,
        external_account_email TEXT,
        display_name TEXT,
        sender_id TEXT,
        sender_name TEXT,
        sender_email TEXT,
        settings_json TEXT,
        provider_metadata_json TEXT,
        is_default BOOLEAN NOT NULL DEFAULT FALSE,
        last_tested_at TIMESTAMPTZ,
        last_error_summary TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (user_id, provider, external_account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_oauth_states (
        id BIGSERIAL PRIMARY KEY,
        state TEXT NOT NULL UNIQUE,
        user_id BIGINT NOT NULL,
        provider TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        code_verifier TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ
    )
    """,
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_email_integrations_user "
    "ON email_integrations(user_id, status, provider)",
    "CREATE INDEX IF NOT EXISTS idx_email_integrations_default "
    "ON email_integrations(user_id, is_default)",
    "CREATE INDEX IF NOT EXISTS idx_email_oauth_states_expires "
    "ON email_oauth_states(expires_at)",
]

EXPORT_COLS_SQLITE = [
    ("email_integration_id", "INTEGER"),
    ("external_account_email", "TEXT"),
    ("connection_display_name", "TEXT"),
    ("draft_type", "TEXT"),
]
EXPORT_COLS_PG = [
    ("email_integration_id", "BIGINT"),
    ("external_account_email", "TEXT"),
    ("connection_display_name", "TEXT"),
    ("draft_type", "TEXT"),
]


def _sqlite_has_column(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {dict(row)["name"] if hasattr(row, "keys") else row[1] for row in rows}
    return column in names


def _postgres_has_column(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return bool(row)


def _add_export_columns(conn, columns, *, postgres):
    for name, sql_type in columns:
        present = (
            _postgres_has_column(conn, "listing_email_campaigns", name)
            if postgres
            else _sqlite_has_column(conn, "listing_email_campaigns", name)
        )
        if not present:
            conn.execute(
                f"ALTER TABLE listing_email_campaigns ADD COLUMN {name} {sql_type}"
            )


def _migrate_sendgrid_connections(conn, *, default_value):
    rows = conn.execute(
        "SELECT * FROM email_marketing_connections ORDER BY user_id, id"
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        external_id = f"legacy-sendgrid-{row['id']}"
        existing = conn.execute(
            """
            SELECT id FROM email_integrations
            WHERE user_id = ? AND provider = 'sendgrid'
              AND external_account_id = ?
            """,
            (row["user_id"], external_id),
        ).fetchone()
        if existing:
            continue
        has_default = conn.execute(
            """
            SELECT id FROM email_integrations
            WHERE user_id = ? AND is_default = ?
            LIMIT 1
            """,
            (row["user_id"], default_value),
        ).fetchone()
        settings = {
            "default_list_ids": json.loads(
                row.get("default_list_ids_json") or "[]"
            ),
            "suppression_group_id": row.get("suppression_group_id"),
            "suppression_group_name": row.get("suppression_group_name"),
        }
        conn.execute(
            """
            INSERT INTO email_integrations (
                user_id, provider, integration_kind, auth_type, status,
                credential_encrypted, external_account_id,
                external_account_email, display_name, sender_id, sender_name,
                sender_email, settings_json, provider_metadata_json,
                is_default, last_tested_at, last_error_summary,
                created_at, updated_at
            ) VALUES (
                ?, 'sendgrid', 'marketing', 'api_key', ?, ?, ?, ?,
                'SendGrid', ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?
            )
            """,
            (
                row["user_id"],
                row.get("status") or "connected",
                row.get("api_key_encrypted"),
                external_id,
                row.get("sender_email"),
                str(row["sender_id"]) if row.get("sender_id") is not None else None,
                row.get("sender_name"),
                row.get("sender_email"),
                json.dumps(settings),
                default_value if not has_default else (False if default_value is True else 0),
                row.get("last_tested_at"),
                row.get("last_error_summary"),
                row.get("created_at"),
                row.get("updated_at"),
            ),
        )


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    _add_export_columns(conn, EXPORT_COLS_SQLITE, postgres=False)
    for sql in INDEXES:
        conn.execute(sql)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_email_integrations_user_default "
        "ON email_integrations(user_id) WHERE is_default = 1"
    )
    _migrate_sendgrid_connections(conn, default_value=1)


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    _add_export_columns(conn, EXPORT_COLS_PG, postgres=True)
    for sql in INDEXES:
        conn.execute(sql)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_email_integrations_user_default "
        "ON email_integrations(user_id) WHERE is_default = TRUE"
    )
    _migrate_sendgrid_connections(conn, default_value=True)
