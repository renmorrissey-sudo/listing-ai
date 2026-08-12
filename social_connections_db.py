"""Tenant-scoped CRUD for social_connections, social_oauth_states, social_publications.

Tokens are always encrypted at rest (see social_tokens.py) and are only ever
decrypted server-side inside social_providers/* at publish time — never
returned from any function callers use to build an HTTP response.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from db import get_db
from db_backend import bind_bool, sql_is_true
from social_tokens import decrypt_token, encrypt_token

OAUTH_STATE_TTL_MINUTES = 10


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


# ---------------------------------------------------------------------------
# OAuth CSRF state
# ---------------------------------------------------------------------------


def create_oauth_state(user_id, provider, *, redirect_uri=None, code_verifier=None):
    state = secrets.token_urlsafe(32)
    now = _now()
    expires_at = now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO social_oauth_states
                (state, user_id, provider, code_verifier, redirect_uri, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (state, user_id, provider, code_verifier, redirect_uri, _iso(now), _iso(expires_at)),
        )
    return state


def consume_oauth_state(state, provider):
    """Validate + single-use-consume a state token. Returns dict or None if invalid/expired/reused."""
    if not state:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM social_oauth_states
            WHERE state = ? AND provider = ? AND consumed_at IS NULL AND expires_at > ?
            """,
            (state, provider, _iso(_now())),
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        conn.execute(
            "UPDATE social_oauth_states SET consumed_at = ? WHERE id = ?",
            (_iso(_now()), row["id"]),
        )
    return row


def cleanup_expired_oauth_states():
    with get_db() as conn:
        conn.execute(
            "DELETE FROM social_oauth_states WHERE expires_at < ?",
            (_iso(_now()),),
        )


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def _connection_public(row):
    """Strip encrypted token columns before returning to any caller that renders UI/JSON."""
    if not row:
        return None
    d = dict(row)
    d.pop("access_token_encrypted", None)
    d.pop("refresh_token_encrypted", None)
    return d


def upsert_connection(
    user_id,
    provider,
    *,
    external_account_id,
    display_name=None,
    access_token=None,
    refresh_token=None,
    token_expires_at=None,
    scopes=None,
):
    """Insert or update the connection for (user_id, provider, external_account_id)."""
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT id FROM social_connections
            WHERE user_id = ? AND provider = ? AND external_account_id = ?
            """,
            (user_id, provider, external_account_id),
        ).fetchone()
        access_enc = encrypt_token(access_token) if access_token else None
        refresh_enc = encrypt_token(refresh_token) if refresh_token else None
        if existing:
            conn_id = existing["id"] if isinstance(existing, dict) else existing[0]
            conn.execute(
                """
                UPDATE social_connections SET
                    display_name = ?, access_token_encrypted = ?, refresh_token_encrypted = ?,
                    token_expires_at = ?, scopes = ?, status = 'connected', updated_at = ?
                WHERE id = ?
                """,
                (display_name, access_enc, refresh_enc, token_expires_at, scopes, _iso(now), conn_id),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO social_connections (
                    user_id, provider, external_account_id, display_name,
                    access_token_encrypted, refresh_token_encrypted, token_expires_at,
                    scopes, status, default_enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'connected', ?, ?, ?)
                """,
                (
                    user_id,
                    provider,
                    external_account_id,
                    display_name,
                    access_enc,
                    refresh_enc,
                    token_expires_at,
                    scopes,
                    bind_bool(False),
                    _iso(now),
                    _iso(now),
                ),
            )
            conn_id = cur.lastrowid
    return get_connection_by_id(user_id, conn_id)


def list_connections(user_id):
    """Tenant-scoped list, newest first. Never includes token material."""
    if not user_id:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM social_connections WHERE user_id = ? ORDER BY provider ASC, id ASC",
            (user_id,),
        ).fetchall()
    return [_connection_public(r) for r in rows]


def get_connection_by_id(user_id, connection_id):
    if not user_id or not connection_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM social_connections WHERE id = ? AND user_id = ?",
            (connection_id, user_id),
        ).fetchone()
    return _connection_public(row)


def get_active_connection(user_id, provider):
    """Tenant-scoped connected row for a provider (public fields only)."""
    if not user_id or not provider:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM social_connections WHERE user_id = ? AND provider = ? AND status = 'connected' "
            "ORDER BY id DESC LIMIT 1",
            (user_id, provider),
        ).fetchone()
    return _connection_public(row)


def get_connection_credentials(user_id, connection_id):
    """Decrypted tokens for internal publish use only. Never expose this dict to the client."""
    if not user_id or not connection_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM social_connections WHERE id = ? AND user_id = ? AND status = 'connected'",
            (connection_id, user_id),
        ).fetchone()
    if not row:
        return None
    row = dict(row)
    return {
        "id": row["id"],
        "provider": row["provider"],
        "external_account_id": row["external_account_id"],
        "access_token": decrypt_token(row.get("access_token_encrypted")),
        "refresh_token": decrypt_token(row.get("refresh_token_encrypted")),
        "token_expires_at": row.get("token_expires_at"),
    }


def set_default_enabled(user_id, connection_id, enabled: bool):
    with get_db() as conn:
        conn.execute(
            "UPDATE social_connections SET default_enabled = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (bind_bool(enabled), _iso(_now()), connection_id, user_id),
        )


def list_default_enabled_connections(user_id):
    """Connected + enabled-for-one-click-post channels for this tenant."""
    if not user_id:
        return []
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM social_connections WHERE user_id = ? AND status = 'connected' "
            f"AND {sql_is_true('default_enabled')}",
            (user_id,),
        ).fetchall()
    return [_connection_public(r) for r in rows]


def disconnect(user_id, connection_id):
    """Deactivate a connection. Tokens are cleared; the row/history is kept for audit."""
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE social_connections SET
                status = 'disconnected', access_token_encrypted = NULL,
                refresh_token_encrypted = NULL, default_enabled = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (bind_bool(False), _iso(_now()), connection_id, user_id),
        )
    return cur.rowcount > 0


def mark_connection_needs_reconnect(connection_id):
    """Flip status when a token is expired/revoked so the UI can prompt reconnect."""
    with get_db() as conn:
        conn.execute(
            "UPDATE social_connections SET status = 'needs_reconnect', updated_at = ? WHERE id = ?",
            (_iso(_now()), connection_id),
        )


# ---------------------------------------------------------------------------
# Publications
# ---------------------------------------------------------------------------


def build_idempotency_key(operation_id, provider):
    """One publish OPERATION (a single Post click, or a single per-channel retry
    click) maps to one idempotency_key per provider. Retrying the exact same
    operation_id (double-click, HTTP retry, job retry) always resolves to the
    same social_publications row via create_publication's existing-row check —
    it never creates a duplicate post. A genuinely new user-initiated Post or
    Retry action must use a new operation_id (see listing_publish.py)."""
    return f"{operation_id}:{provider}"


def new_operation_id():
    return uuid.uuid4().hex


def create_publication(user_id, listing_generation_id, provider, social_connection_id, idempotency_key):
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM social_publications WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return dict(existing)
        cur = conn.execute(
            """
            INSERT INTO social_publications (
                user_id, listing_generation_id, provider, social_connection_id,
                status, idempotency_key, requested_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (user_id, listing_generation_id, provider, social_connection_id, idempotency_key, _iso(now)),
        )
        pub_id = cur.lastrowid
        row = conn.execute("SELECT * FROM social_publications WHERE id = ?", (pub_id,)).fetchone()
    return dict(row)


def update_publication_result(
    publication_id,
    *,
    status,
    provider_post_id=None,
    provider_post_url=None,
    error_code=None,
    error_summary=None,
):
    published_at = _iso(_now()) if status == "published" else None
    with get_db() as conn:
        conn.execute(
            """
            UPDATE social_publications SET
                status = ?, provider_post_id = ?, provider_post_url = ?,
                error_code = ?, error_summary = ?, published_at = ?
            WHERE id = ?
            """,
            (status, provider_post_id, provider_post_url, error_code, error_summary, published_at, publication_id),
        )


def list_publications_for_generation(user_id, listing_generation_id):
    if not user_id or not listing_generation_id:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM social_publications WHERE user_id = ? AND listing_generation_id = ? "
            "ORDER BY id ASC",
            (user_id, listing_generation_id),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_publications_by_provider(user_id, listing_generation_id):
    """{provider: latest publication row} — most recent attempt wins for status display."""
    rows = list_publications_for_generation(user_id, listing_generation_id)
    latest = {}
    for row in rows:
        latest[row["provider"]] = row
    return latest


def publications_for_generations(user_id, listing_generation_ids):
    """{generation_id: {provider: status}} for a batch of generations (archive list view)."""
    if not user_id or not listing_generation_ids:
        return {}
    placeholders = ",".join("?" for _ in listing_generation_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM social_publications WHERE user_id = ? "
            f"AND listing_generation_id IN ({placeholders}) ORDER BY id ASC",
            [user_id] + list(listing_generation_ids),
        ).fetchall()
    result: dict = {}
    for row in rows:
        row = dict(row)
        gen_id = row["listing_generation_id"]
        result.setdefault(gen_id, {})[row["provider"]] = row["status"]
    return result
