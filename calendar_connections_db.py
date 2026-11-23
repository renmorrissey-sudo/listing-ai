"""Tenant-scoped CRUD for calendar_connections + calendar_oauth_states.

Mirrors the social pattern (social_connections_db): per-tenant OAuth
connections with Fernet-encrypted tokens (integration_credentials
helpers, keyed by INTEGRATION_CREDENTIAL_ENCRYPTION_KEY, falling back
to SOCIAL_TOKEN_ENCRYPTION_KEY), short-lived CSRF state tokens for the
OAuth round trip, and a mock connection for staging/test.

Mock selection rule (Phase III-4 §8n): the mock provider is selected
by the resolved mock transport (``config.HARNESS_MOCK_PROVIDERS``),
never by "is staging" and never by default. A mock connection resolves
to no provider when the mock transport is off, so it can never run in
production. Tokens are only ever decrypted server-side inside
calendar_providers/* at call time — never returned to a UI/JSON caller.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import config
from db import get_db
from integration_credentials import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

OAUTH_STATE_TTL_MINUTES = 10
TOKEN_REFRESH_LEAD_MINUTES = 5


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
            INSERT INTO calendar_oauth_states
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
            SELECT * FROM calendar_oauth_states
            WHERE state = ? AND provider = ? AND consumed_at IS NULL AND expires_at > ?
            """,
            (state, provider, _iso(_now())),
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        conn.execute(
            "UPDATE calendar_oauth_states SET consumed_at = ? WHERE id = ?",
            (_iso(_now()), row["id"]),
        )
    return row


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
    calendar_id=None,
):
    """Insert or update the connection for (user_id, provider, external_account_id)."""
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT id FROM calendar_connections
            WHERE user_id = ? AND provider = ? AND external_account_id = ?
            """,
            (user_id, provider, external_account_id),
        ).fetchone()
        access_enc = encrypt_secret(access_token) if access_token else None
        refresh_enc = encrypt_secret(refresh_token) if refresh_token else None
        if existing:
            conn_id = existing["id"] if isinstance(existing, dict) else existing[0]
            conn.execute(
                """
                UPDATE calendar_connections SET
                    display_name = ?, access_token_encrypted = ?, refresh_token_encrypted = ?,
                    token_expires_at = ?, scopes = ?, calendar_id = ?,
                    status = 'connected', updated_at = ?
                WHERE id = ?
                """,
                (
                    display_name,
                    access_enc,
                    refresh_enc,
                    token_expires_at,
                    scopes,
                    calendar_id or "primary",
                    _iso(now),
                    conn_id,
                ),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO calendar_connections (
                    user_id, provider, external_account_id, display_name,
                    access_token_encrypted, refresh_token_encrypted, token_expires_at,
                    scopes, calendar_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'connected', ?, ?)
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
                    calendar_id or "primary",
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
            "SELECT * FROM calendar_connections WHERE user_id = ? ORDER BY provider ASC, id ASC",
            (user_id,),
        ).fetchall()
    return [_connection_public(r) for r in rows]


def get_connection_by_id(user_id, connection_id):
    if not user_id or not connection_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM calendar_connections WHERE id = ? AND user_id = ?",
            (connection_id, user_id),
        ).fetchone()
    return _connection_public(row)


def get_active_connection(user_id):
    """The tenant's connected calendar (any provider, newest first). Public fields only."""
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM calendar_connections WHERE user_id = ? AND status = 'connected' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return _connection_public(row)


def disconnect(user_id, connection_id):
    """Deactivate a connection. Tokens are cleared; the row/history is kept for audit."""
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE calendar_connections SET
                status = 'disconnected', access_token_encrypted = NULL,
                refresh_token_encrypted = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (_iso(_now()), connection_id, user_id),
        )
    return cur.rowcount > 0


def mark_connection_token_expired(connection_id):
    """Flip status when a token refresh fails so the UI can prompt reconnect."""
    with get_db() as conn:
        conn.execute(
            "UPDATE calendar_connections SET status = 'token_expired', updated_at = ? WHERE id = ?",
            (_iso(_now()), connection_id),
        )


def _flag_token_expired(user_id, connection_id):
    """Surface the expired connection as a visible needs-attention item (lead-less)."""
    try:
        import crm_db

        crm_db.upsert_needs_attention(
            user_id,
            None,
            "calendar_connection_expired",
            priority="high",
            source_ref_type="calendar_connection",
            source_ref_id=connection_id,
            reason_text="Calendar connection expired — reconnect to confirm bookings.",
        )
    except Exception:
        logger.warning(
            "calendar connection expired flag failed tenant=%s connection=%s",
            user_id,
            connection_id,
            exc_info=True,
        )


def get_connection_credentials(user_id, connection_id):
    """Decrypted tokens for internal provider use only. Refresh-on-use:
    when the access token expires within 5 minutes, the provider's
    refresh token is exchanged and the row is updated in the same call.
    A refresh failure marks the connection ``token_expired``, surfaces a
    needs-attention item, and returns None. Never expose this dict to
    the client."""
    if not user_id or not connection_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM calendar_connections WHERE id = ? AND user_id = ? AND status = 'connected'",
            (connection_id, user_id),
        ).fetchone()
    if not row:
        return None
    row = dict(row)
    credentials = {
        "id": row["id"],
        "user_id": user_id,
        "provider": row["provider"],
        "external_account_id": row["external_account_id"],
        "access_token": decrypt_secret(row.get("access_token_encrypted")),
        "refresh_token": decrypt_secret(row.get("refresh_token_encrypted")),
        "token_expires_at": row.get("token_expires_at"),
        "calendar_id": row.get("calendar_id") or "primary",
    }
    if row["provider"] == "mock":
        return credentials
    expires_at = credentials["token_expires_at"]
    needs_refresh = True
    if expires_at:
        try:
            parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            needs_refresh = parsed <= _now() + timedelta(minutes=TOKEN_REFRESH_LEAD_MINUTES)
        except (TypeError, ValueError):
            needs_refresh = True
    if not needs_refresh:
        return credentials
    refresh_token = credentials["refresh_token"]
    if not refresh_token:
        mark_connection_token_expired(row["id"])
        _flag_token_expired(user_id, row["id"])
        return None
    try:
        from calendar_providers.registry import get_provider

        provider = get_provider(row["provider"])
        refreshed = provider.refresh_access_token(refresh_token=refresh_token)
    except Exception:
        logger.warning(
            "calendar token refresh failed tenant=%s connection=%s",
            user_id,
            row["id"],
            exc_info=True,
        )
        mark_connection_token_expired(row["id"])
        _flag_token_expired(user_id, row["id"])
        return None
    new_access = refreshed.get("access_token")
    if not new_access:
        mark_connection_token_expired(row["id"])
        _flag_token_expired(user_id, row["id"])
        return None
    with get_db() as conn:
        conn.execute(
            """
            UPDATE calendar_connections SET
                access_token_encrypted = ?, token_expires_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                encrypt_secret(new_access),
                refreshed.get("expires_at"),
                _iso(_now()),
                row["id"],
                user_id,
            ),
        )
    credentials["access_token"] = new_access
    credentials["token_expires_at"] = refreshed.get("expires_at")
    return credentials


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_calendar_provider(user_id):
    """Resolve the tenant's calendar provider + fresh credentials.

    Returns ``(provider_instance, credentials_dict)`` or ``(None, None)``
    when no calendar is connected. A ``mock`` connection resolves only
    while the mock transport is on (``config.HARNESS_MOCK_PROVIDERS``);
    with the mock transport off it resolves to no provider, so the mock
    can never run in production.
    """
    from calendar_providers.registry import get_provider

    connection = get_active_connection(user_id)
    if not connection:
        return None, None
    provider_name = (connection.get("provider") or "").strip().lower()
    if provider_name == "mock":
        if not getattr(config, "HARNESS_MOCK_PROVIDERS", False):
            logger.warning(
                "mock calendar connection ignored tenant=%s (mock transport off)",
                user_id,
            )
            return None, None
        provider = get_provider("mock")
        return provider, {
            "id": connection["id"],
            "user_id": user_id,
            "provider": "mock",
            "external_account_id": connection.get("external_account_id") or "mock",
            "access_token": "mock-access-token",
            "refresh_token": None,
            "token_expires_at": None,
            "calendar_id": connection.get("calendar_id") or "primary",
        }
    provider = get_provider(provider_name)
    if not provider:
        return None, None
    credentials = get_connection_credentials(user_id, connection["id"])
    if not credentials:
        return None, None
    return provider, credentials


def ensure_mock_connection(user_id):
    """Create (or return) the tenant's mock calendar connection.

    Refused unless the mock transport is on — the mock must never be
    connected in production. Used by tests and the staging setup step.
    Returns the public connection row, or None when refused.
    """
    if not getattr(config, "HARNESS_MOCK_PROVIDERS", False):
        return None
    existing = get_active_connection(user_id)
    if existing and (existing.get("provider") or "") == "mock":
        return existing
    return upsert_connection(
        user_id,
        "mock",
        external_account_id="mock",
        display_name="Mock Calendar",
        calendar_id="primary",
    )
