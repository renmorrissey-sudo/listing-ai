"""Tenant-scoped SendGrid Marketing settings and listing draft exports."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from db import get_db
from integration_credentials import decrypt_secret, encrypt_secret


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


def connect(user_id, api_key, *, provider="sendgrid"):
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
    return get_connection(user_id, provider)


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
    return get_connection(user_id, provider)


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
