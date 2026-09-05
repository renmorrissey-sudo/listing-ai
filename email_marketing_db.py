"""Tenant-scoped SendGrid Marketing settings and listing draft exports."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from db import get_db
from db_backend import bind_bool
from integration_credentials import decrypt_secret, encrypt_secret

OAUTH_STATE_TTL_MINUTES = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads_list(value) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if item]


def _public_connection(row):
    if not row:
        return None
    data = dict(row)
    data.pop("api_key_encrypted", None)
    data["default_list_ids"] = _loads_list(
        data.pop("default_list_ids_json", None)
    )
    return data


def get_connection(user_id, provider="sendgrid"):
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM email_marketing_connections
            WHERE user_id = ? AND provider = ?
            """,
            (user_id, provider),
        ).fetchone()
    return _public_connection(row)


def get_credentials(user_id, provider="sendgrid"):
    """Internal-only credentials. Never pass this return value to a response."""
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM email_marketing_connections
            WHERE user_id = ? AND provider = ? AND status = 'connected'
            """,
            (user_id, provider),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    api_key = decrypt_secret(data.get("api_key_encrypted"))
    if not api_key:
        return None
    return {
        "id": data["id"],
        "user_id": data["user_id"],
        "provider": data["provider"],
        "api_key": api_key,
        "sender_id": data.get("sender_id"),
        "sender_name": data.get("sender_name"),
        "sender_email": data.get("sender_email"),
        "default_list_ids": _loads_list(data.get("default_list_ids_json")),
        "suppression_group_id": data.get("suppression_group_id"),
        "suppression_group_name": data.get("suppression_group_name"),
    }


def connect(user_id, api_key, *, provider="sendgrid", mirror_generic=True):
    if not user_id:
        raise ValueError("user_id is required")
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("api_key is required")
    encrypted = encrypt_secret(api_key)
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT id FROM email_marketing_connections
            WHERE user_id = ? AND provider = ?
            """,
            (user_id, provider),
        ).fetchone()
        if existing:
            connection_id = dict(existing)["id"]
            conn.execute(
                """
                UPDATE email_marketing_connections
                SET api_key_encrypted = ?, status = 'connected',
                    last_error_summary = NULL, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (encrypted, now, connection_id, user_id),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO email_marketing_connections (
                    user_id, provider, api_key_encrypted, status,
                    default_list_ids_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'connected', '[]', ?, ?)
                """,
                (user_id, provider, encrypted, now, now),
            )
            connection_id = cur.lastrowid
    connection = get_connection(user_id, provider)
    if provider == "sendgrid" and mirror_generic:
        upsert_integration(
            user_id,
            "sendgrid",
            integration_kind="marketing",
            auth_type="api_key",
            external_account_id=f"legacy-sendgrid-{connection_id}",
            display_name="SendGrid",
            credential=api_key,
        )
    return connection


def save_settings(
    user_id,
    *,
    sender_id=None,
    sender_name=None,
    sender_email=None,
    default_list_ids=None,
    suppression_group_id=None,
    suppression_group_name=None,
    provider="sendgrid",
    mirror_generic=True,
):
    list_ids = [str(item) for item in (default_list_ids or []) if item]
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE email_marketing_connections
            SET sender_id = ?, sender_name = ?, sender_email = ?,
                default_list_ids_json = ?, suppression_group_id = ?,
                suppression_group_name = ?, updated_at = ?
            WHERE user_id = ? AND provider = ? AND status = 'connected'
            """,
            (
                int(sender_id) if sender_id not in (None, "") else None,
                sender_name,
                sender_email,
                json.dumps(list_ids),
                (
                    int(suppression_group_id)
                    if suppression_group_id not in (None, "")
                    else None
                ),
                suppression_group_name,
                now,
                user_id,
                provider,
            ),
        )
    if cur.rowcount <= 0:
        raise ValueError("No connected email marketing account.")
    connection = get_connection(user_id, provider)
    if provider == "sendgrid" and mirror_generic:
        integrations = [
            item
            for item in list_integrations(user_id)
            if item["provider"] == "sendgrid"
            and item["external_account_id"]
            == f"legacy-sendgrid-{connection['id']}"
        ]
        if integrations:
            save_integration_settings(
                user_id,
                integrations[0]["id"],
                sender_id=sender_id,
                sender_name=sender_name,
                sender_email=sender_email,
                settings={
                    "default_list_ids": list_ids,
                    "suppression_group_id": suppression_group_id,
                    "suppression_group_name": suppression_group_name,
                },
            )
    return connection


def mark_test_result(user_id, *, error_summary=None, provider="sendgrid"):
    now = _now()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE email_marketing_connections
            SET last_tested_at = ?, last_error_summary = ?, updated_at = ?
            WHERE user_id = ? AND provider = ?
            """,
            (now, error_summary, now, user_id, provider),
        )
    if provider == "sendgrid":
        for integration in list_integrations(user_id):
            if integration["provider"] == "sendgrid":
                mark_integration_test_result(
                    user_id,
                    integration["id"],
                    error_summary=error_summary,
                )


def disconnect(user_id, provider="sendgrid"):
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE email_marketing_connections
            SET api_key_encrypted = NULL, status = 'disconnected',
                sender_id = NULL, sender_name = NULL, sender_email = NULL,
                default_list_ids_json = '[]',
                suppression_group_id = NULL, suppression_group_name = NULL,
                updated_at = ?
            WHERE user_id = ? AND provider = ?
            """,
            (now, user_id, provider),
        )
    if provider == "sendgrid":
        for integration in list_integrations(user_id):
            if integration["provider"] == "sendgrid":
                disconnect_integration(user_id, integration["id"])
    return cur.rowcount > 0


def _export_key(user_id, listing_generation_id, *, create_another=False):
    if create_another:
        suffix = uuid.uuid4().hex
    else:
        suffix = "primary"
    return f"listing-email:{user_id}:{listing_generation_id}:sendgrid:{suffix}"


def create_or_get_export(
    user_id,
    listing_generation_id,
    *,
    property_address,
    subject,
    create_another=False,
):
    """Reserve an export row before the provider call.

    Returns ``(row, created)``. The deterministic primary key prevents double
    clicks and HTTP retries from creating multiple remote drafts.
    """
    key = _export_key(
        user_id, listing_generation_id, create_another=create_another
    )
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT * FROM listing_email_campaigns
            WHERE user_id = ? AND idempotency_key = ?
            """,
            (user_id, key),
        ).fetchone()
        if existing:
            return dict(existing), False
        cur = conn.execute(
            """
            INSERT INTO listing_email_campaigns (
                user_id, listing_generation_id, provider,
                property_address, subject, status, idempotency_key,
                created_at, updated_at
            ) VALUES (?, ?, 'sendgrid', ?, ?, 'creating', ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                user_id,
                listing_generation_id,
                property_address,
                subject,
                key,
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM listing_email_campaigns
            WHERE user_id = ? AND idempotency_key = ?
            """,
            (user_id, key),
        ).fetchone()
    return dict(row), cur.rowcount > 0


def update_export(
    user_id,
    export_id,
    *,
    status,
    provider_campaign_id=None,
    provider_status=None,
    error_code=None,
    error_summary=None,
):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE listing_email_campaigns
            SET status = ?, provider_campaign_id = ?, provider_status = ?,
                error_code = ?, error_summary = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                status,
                provider_campaign_id,
                provider_status,
                error_code,
                error_summary,
                _now(),
                export_id,
                user_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM listing_email_campaigns WHERE id = ? AND user_id = ?",
            (export_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_for_generation(user_id, listing_generation_id):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listing_email_campaigns
            WHERE user_id = ? AND listing_generation_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id, listing_generation_id),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_for_generation(user_id, listing_generation_id):
    rows = list_for_generation(user_id, listing_generation_id)
    return rows[0] if rows else None


def campaigns_for_generations(user_id, generation_ids):
    if not user_id or not generation_ids:
        return {}
    placeholders = ",".join("?" for _ in generation_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM listing_email_campaigns
            WHERE user_id = ? AND listing_generation_id IN ({placeholders})
            ORDER BY created_at ASC, id ASC
            """,
            [user_id] + list(generation_ids),
        ).fetchall()
    result = {}
    for row in rows:
        data = dict(row)
        result[data["listing_generation_id"]] = data
    return result


def public_export(row):
    if not row:
        return None
    return {
        "id": row.get("id"),
        "provider": row.get("provider"),
        "provider_campaign_id": row.get("provider_campaign_id"),
        "status": row.get("status"),
        "provider_status": row.get("provider_status"),
        "error_summary": row.get("error_summary"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def annotate_campaign_status(user_id, generations):
    ids = [item["id"] for item in generations]
    status_map = campaigns_for_generations(user_id, ids)
    for generation in generations:
        generation["email_campaign"] = public_export(
            status_map.get(generation["id"])
        )
    return generations


# ---------------------------------------------------------------------------
# Generic user-owned email integrations (migration 021+)
# ---------------------------------------------------------------------------


def _json_dict(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _public_integration(row):
    if not row:
        return None
    data = dict(row)
    for key in (
        "credential_encrypted",
        "access_token_encrypted",
        "refresh_token_encrypted",
    ):
        data.pop(key, None)
    data["settings"] = _json_dict(data.pop("settings_json", None))
    data["provider_metadata"] = _json_dict(
        data.pop("provider_metadata_json", None)
    )
    data["is_default"] = bool(data.get("is_default"))
    return data


def list_integrations(user_id, *, connected_only=False):
    if not user_id:
        return []
    sql = "SELECT * FROM email_integrations WHERE user_id = ?"
    params = [user_id]
    if connected_only:
        sql += " AND status = 'connected'"
    sql += " ORDER BY is_default DESC, provider ASC, id ASC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_public_integration(row) for row in rows]


def get_integration(user_id, integration_id):
    if not user_id or not integration_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM email_integrations WHERE id = ? AND user_id = ?",
            (integration_id, user_id),
        ).fetchone()
    return _public_integration(row)


def get_default_integration(user_id):
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM email_integrations
            WHERE user_id = ? AND status = 'connected' AND is_default = ?
            ORDER BY id ASC LIMIT 1
            """,
            (user_id, bind_bool(True)),
        ).fetchone()
        if not row:
            row = conn.execute(
                """
                SELECT * FROM email_integrations
                WHERE user_id = ? AND status = 'connected'
                ORDER BY id ASC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
    return _public_integration(row)


def get_integration_credentials(user_id, integration_id):
    if not user_id or not integration_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM email_integrations
            WHERE id = ? AND user_id = ? AND status = 'connected'
            """,
            (integration_id, user_id),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    return {
        **_public_integration(data),
        "credential": decrypt_secret(data.get("credential_encrypted")),
        "access_token": decrypt_secret(data.get("access_token_encrypted")),
        "refresh_token": decrypt_secret(data.get("refresh_token_encrypted")),
    }


def upsert_integration(
    user_id,
    provider,
    *,
    integration_kind,
    auth_type,
    external_account_id,
    external_account_email=None,
    display_name=None,
    credential=None,
    access_token=None,
    refresh_token=None,
    token_expires_at=None,
    scopes=None,
    sender_id=None,
    sender_name=None,
    sender_email=None,
    settings=None,
    provider_metadata=None,
):
    if not user_id or not provider or not external_account_id:
        raise ValueError("user_id, provider, and external_account_id are required")
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT * FROM email_integrations
            WHERE user_id = ? AND provider = ? AND external_account_id = ?
            """,
            (user_id, provider, str(external_account_id)),
        ).fetchone()
        existing_data = dict(existing) if existing else {}
        credential_enc = (
            encrypt_secret(credential)
            if credential
            else existing_data.get("credential_encrypted")
        )
        access_enc = (
            encrypt_secret(access_token)
            if access_token
            else existing_data.get("access_token_encrypted")
        )
        refresh_enc = (
            encrypt_secret(refresh_token)
            if refresh_token
            else existing_data.get("refresh_token_encrypted")
        )
        if existing:
            integration_id = existing_data["id"]
            conn.execute(
                """
                UPDATE email_integrations SET
                    integration_kind = ?, auth_type = ?, status = 'connected',
                    credential_encrypted = ?, access_token_encrypted = ?,
                    refresh_token_encrypted = ?, token_expires_at = ?, scopes = ?,
                    external_account_email = ?, display_name = ?, sender_id = ?,
                    sender_name = ?, sender_email = ?, settings_json = ?,
                    provider_metadata_json = ?, last_error_summary = NULL,
                    updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    integration_kind,
                    auth_type,
                    credential_enc,
                    access_enc,
                    refresh_enc,
                    token_expires_at,
                    scopes,
                    external_account_email,
                    display_name,
                    str(sender_id) if sender_id is not None else None,
                    sender_name,
                    sender_email,
                    json.dumps(settings or {}),
                    json.dumps(provider_metadata or {}),
                    now,
                    integration_id,
                    user_id,
                ),
            )
        else:
            has_any = conn.execute(
                """
                SELECT id FROM email_integrations
                WHERE user_id = ? AND status = 'connected' LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            cur = conn.execute(
                """
                INSERT INTO email_integrations (
                    user_id, provider, integration_kind, auth_type, status,
                    credential_encrypted, access_token_encrypted,
                    refresh_token_encrypted, token_expires_at, scopes,
                    external_account_id, external_account_email, display_name,
                    sender_id, sender_name, sender_email, settings_json,
                    provider_metadata_json, is_default, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    user_id,
                    provider,
                    integration_kind,
                    auth_type,
                    credential_enc,
                    access_enc,
                    refresh_enc,
                    token_expires_at,
                    scopes,
                    str(external_account_id),
                    external_account_email,
                    display_name,
                    str(sender_id) if sender_id is not None else None,
                    sender_name,
                    sender_email,
                    json.dumps(settings or {}),
                    json.dumps(provider_metadata or {}),
                    bind_bool(not has_any),
                    now,
                    now,
                ),
            )
            integration_id = cur.lastrowid
    return get_integration(user_id, integration_id)


def update_integration_tokens(
    user_id,
    integration_id,
    *,
    access_token,
    refresh_token=None,
    token_expires_at=None,
    scopes=None,
):
    current = get_integration_credentials(user_id, integration_id)
    if not current:
        return None
    with get_db() as conn:
        conn.execute(
            """
            UPDATE email_integrations SET
                access_token_encrypted = ?, refresh_token_encrypted = ?,
                token_expires_at = ?, scopes = COALESCE(?, scopes),
                status = 'connected', last_error_summary = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                encrypt_secret(access_token),
                encrypt_secret(refresh_token or current.get("refresh_token")),
                token_expires_at,
                scopes,
                _now(),
                integration_id,
                user_id,
            ),
        )
    return get_integration(user_id, integration_id)


def save_integration_settings(
    user_id,
    integration_id,
    *,
    sender_id=None,
    sender_name=None,
    sender_email=None,
    settings=None,
):
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE email_integrations SET sender_id = ?, sender_name = ?,
                sender_email = ?, settings_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'connected'
            """,
            (
                str(sender_id) if sender_id not in (None, "") else None,
                sender_name,
                sender_email,
                json.dumps(settings or {}),
                _now(),
                integration_id,
                user_id,
            ),
        )
    if cur.rowcount <= 0:
        raise ValueError("Email integration not found.")
    return get_integration(user_id, integration_id)


def set_default_integration(user_id, integration_id):
    owned = get_integration(user_id, integration_id)
    if not owned or owned.get("status") != "connected":
        return False
    with get_db() as conn:
        conn.execute(
            "UPDATE email_integrations SET is_default = ? WHERE user_id = ?",
            (bind_bool(False), user_id),
        )
        conn.execute(
            """
            UPDATE email_integrations SET is_default = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (bind_bool(True), _now(), integration_id, user_id),
        )
    return True


def mark_integration_test_result(user_id, integration_id, *, error_summary=None):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE email_integrations SET last_tested_at = ?,
                last_error_summary = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (_now(), error_summary, _now(), integration_id, user_id),
        )


def mark_integration_needs_reconnect(user_id, integration_id, error_summary):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE email_integrations SET status = 'needs_reconnect',
                last_error_summary = ?, is_default = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                error_summary,
                bind_bool(False),
                _now(),
                integration_id,
                user_id,
            ),
        )


def disconnect_integration(user_id, integration_id):
    current = get_integration(user_id, integration_id)
    if not current:
        return False
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE email_integrations SET status = 'disconnected',
                credential_encrypted = NULL, access_token_encrypted = NULL,
                refresh_token_encrypted = NULL, token_expires_at = NULL,
                is_default = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (bind_bool(False), _now(), integration_id, user_id),
        )
    if current.get("is_default"):
        remaining = list_integrations(user_id, connected_only=True)
        if remaining:
            set_default_integration(user_id, remaining[0]["id"])
    return cur.rowcount > 0


def create_oauth_state(
    user_id, provider, *, redirect_uri, code_verifier=None
):
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO email_oauth_states (
                state, user_id, provider, redirect_uri, code_verifier,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state,
                user_id,
                provider,
                redirect_uri,
                code_verifier,
                now.isoformat(),
                (now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES)).isoformat(),
            ),
        )
    return state


def consume_oauth_state(state, provider, user_id):
    if not state or not user_id:
        return None
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM email_oauth_states
            WHERE state = ? AND provider = ? AND user_id = ?
              AND consumed_at IS NULL
              AND expires_at > ?
            """,
            (state, provider, user_id, now),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        cur = conn.execute(
            """
            UPDATE email_oauth_states SET consumed_at = ?
            WHERE id = ? AND user_id = ? AND consumed_at IS NULL
            """,
            (now, data["id"], user_id),
        )
        if cur.rowcount <= 0:
            return None
    return data


def create_or_get_integration_export(
    user_id,
    listing_generation_id,
    integration,
    *,
    property_address,
    subject,
    create_another=False,
):
    suffix = uuid.uuid4().hex if create_another else "primary"
    key = (
        f"listing-email:{user_id}:{listing_generation_id}:"
        f"integration:{integration['id']}:{suffix}"
    )
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO listing_email_campaigns (
                user_id, listing_generation_id, email_integration_id, provider,
                external_account_email, connection_display_name, draft_type,
                property_address, subject, status, idempotency_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                user_id,
                listing_generation_id,
                integration["id"],
                integration["provider"],
                integration.get("external_account_email"),
                integration.get("display_name"),
                (
                    "mailbox"
                    if integration.get("integration_kind") == "personal"
                    else "campaign"
                ),
                property_address,
                subject,
                key,
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM listing_email_campaigns
            WHERE user_id = ? AND idempotency_key = ?
            """,
            (user_id, key),
        ).fetchone()
    return dict(row), cur.rowcount > 0


def public_integration_export(row):
    data = public_export(row)
    if not data:
        return None
    data.update(
        {
            "email_integration_id": row.get("email_integration_id"),
            "external_account_email": row.get("external_account_email"),
            "connection_display_name": row.get("connection_display_name"),
            "draft_type": row.get("draft_type"),
        }
    )
    return data


def annotate_integration_campaign_status(user_id, generations):
    if not generations:
        return generations
    ids = [item["id"] for item in generations]
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM listing_email_campaigns
            WHERE user_id = ? AND listing_generation_id IN ({placeholders})
            ORDER BY created_at ASC, id ASC
            """,
            [user_id] + ids,
        ).fetchall()
    grouped = {}
    for row in rows:
        data = dict(row)
        grouped.setdefault(data["listing_generation_id"], []).append(
            public_integration_export(data)
        )
    for generation in generations:
        campaigns = grouped.get(generation["id"], [])
        generation["email_campaigns"] = campaigns
        generation["email_campaign"] = campaigns[-1] if campaigns else None
    return generations
