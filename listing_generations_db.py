"""Persistent Listing Generator history: auto-save, Recent 20, Archive, versions.

Every row is tenant-scoped by `user_id` (the app's only tenant identifier —
see AGENTS.md). All read queries enforce `expires_at > now` in SQL
(defense-in-depth #1); `cleanup_expired()` is defense-in-depth #2, a hard
delete run periodically by the worker (see workers/sms_campaign_worker.py).

`output_snapshot` is stored verbatim (JSON) so reopening a past generation
restores the exact content the user saw — it never re-derives or
regenerates.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import config
from address_normalize import normalize_address_key
from db import get_db


def _now_dt():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _retention_days():
    try:
        return int(config.LISTING_GENERATION_RETENTION_DAYS)
    except (TypeError, ValueError):
        return 60


def _loads(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    d["input_snapshot"] = _loads(d.pop("input_snapshot_json", None))
    d["output_snapshot"] = _loads(d.pop("output_snapshot_json", None))
    d["social_content"] = _loads(d.pop("social_content_json", None))
    return d


def create_generation(
    user_id,
    *,
    display_address: str,
    output_snapshot: dict,
    input_snapshot: dict | None = None,
    social_content: dict | None = None,
    status: str = "completed",
):
    """Persist one successful Listing Generator run. Never overwrites prior versions."""
    if not user_id:
        raise ValueError("user_id is required")
    if not (display_address or "").strip():
        raise ValueError("display_address is required")
    if not output_snapshot:
        raise ValueError("output_snapshot is required")

    now = _now_dt()
    expires_at = now + timedelta(days=_retention_days())
    normalized = normalize_address_key(display_address)

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO listing_generations (
                user_id, display_address, normalized_address,
                input_snapshot_json, output_snapshot_json, social_content_json,
                status, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                display_address.strip(),
                normalized,
                json.dumps(input_snapshot) if input_snapshot is not None else None,
                json.dumps(output_snapshot),
                json.dumps(social_content) if social_content is not None else None,
                status,
                _iso(now),
                _iso(now),
                _iso(expires_at),
            ),
        )
        generation_id = cur.lastrowid
    return get_by_id(user_id, generation_id)


def get_by_id(user_id, generation_id):
    """Tenant-scoped fetch. Returns None if missing, expired, or owned by another tenant."""
    if not user_id or not generation_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM listing_generations
            WHERE id = ? AND user_id = ? AND expires_at > ?
            """,
            (generation_id, user_id, _iso(_now_dt())),
        ).fetchone()
    return _row_to_dict(row)


def list_recent(user_id, limit=20):
    """Newest-first, capped at `limit` (default 20), within the retention window."""
    if not user_id:
        return []
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listing_generations
            WHERE user_id = ? AND expires_at > ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, _iso(_now_dt()), int(limit)),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_archive(user_id, query: str | None = None, page: int = 1, page_size: int = 20):
    """Paginated archive within the retention window, newest first.

    Mirrors crm_db's filter/count pagination convention. `query` matches
    against the address as typed by the user (LIKE, tenant-scoped).
    """
    if not user_id:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size}
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    offset = (page - 1) * page_size

    sql = "FROM listing_generations WHERE user_id = ? AND expires_at > ?"
    params = [user_id, _iso(_now_dt())]
    term = (query or "").strip()
    if term:
        sql += " AND (display_address LIKE ? OR normalized_address LIKE ?)"
        like_term = f"%{term}%"
        like_norm = f"%{normalize_address_key(term)}%"
        params.extend([like_term, like_norm])

    with get_db() as conn:
        total_row = conn.execute(f"SELECT COUNT(*) AS c {sql}", params).fetchone()
        total = int(total_row["c"] if isinstance(total_row, dict) else total_row[0])
        rows = conn.execute(
            f"SELECT * {sql} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
    return {
        "items": [_row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def list_versions_for_address(user_id, normalized_address: str):
    """All retained generations sharing the same normalized address, newest first."""
    if not user_id or not normalized_address:
        return []
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listing_generations
            WHERE user_id = ? AND normalized_address = ? AND expires_at > ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id, normalized_address, _iso(_now_dt())),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def cleanup_expired():
    """Hard-delete rows past their retention window, cascading social_publications.

    No shared assets exist to worry about (Listing Generator has no image
    storage), so this is a plain delete-by-id sweep. Returns the number of
    listing_generations rows deleted.
    """
    now_iso = _iso(_now_dt())
    with get_db() as conn:
        expired_rows = conn.execute(
            "SELECT id FROM listing_generations WHERE expires_at < ?",
            (now_iso,),
        ).fetchall()
        expired_ids = [r["id"] if isinstance(r, dict) else r[0] for r in expired_rows]
        if not expired_ids:
            return 0
        for gen_id in expired_ids:
            conn.execute(
                "DELETE FROM social_publications WHERE listing_generation_id = ?",
                (gen_id,),
            )
            conn.execute("DELETE FROM listing_generations WHERE id = ?", (gen_id,))
    return len(expired_ids)
