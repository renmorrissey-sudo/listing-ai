"""Tenant-scoped persistence and Target Area search for Call Script runs."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from db import get_db

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_target_area(value):
    cleaned = _PUNCTUATION_RE.sub(" ", str(value or "").lower())
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _row_to_dict(row):
    return dict(row) if row else None


def create_generation(user_id, *, inputs, outputs):
    target_area = str((inputs or {}).get("area") or "").strip()
    if not user_id or not target_area:
        raise ValueError("user_id and target area are required")
    if not (outputs or {}).get("opening"):
        raise ValueError("opening script is required")

    created_at = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO call_script_generations (
                user_id, target_area, normalized_target_area, property_type,
                situation, agent_name, key_benefit, area_overview,
                opening_script, objection_handlers, voicemail_script, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                target_area,
                normalize_target_area(target_area),
                inputs.get("property_type"),
                inputs.get("situation"),
                inputs.get("agent_name"),
                inputs.get("key_benefit"),
                outputs.get("overview") or "",
                outputs.get("opening") or "",
                outputs.get("objections") or "",
                outputs.get("voicemail") or "",
                created_at,
            ),
        )
        generation_id = cur.lastrowid
    return get_by_id(user_id, generation_id)


def get_by_id(user_id, generation_id):
    if not user_id or not generation_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM call_script_generations WHERE id = ? AND user_id = ?",
            (generation_id, user_id),
        ).fetchone()
    return _row_to_dict(row)


def search_history(user_id, area=None, limit=50):
    if not user_id:
        return []
    limit = max(1, min(100, int(limit or 50)))
    sql = "SELECT * FROM call_script_generations WHERE user_id = ?"
    params = [user_id]
    term = normalize_target_area(area)
    if term:
        sql += " AND normalized_target_area LIKE ?"
        params.append(f"%{term}%")
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]
