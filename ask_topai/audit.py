"""Persist Ask TopAI interpret/execute events. No secrets."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import crm_db
from db import get_db

SOURCE = "ask_topai"
TOKEN_TTL_MINUTES = 15


def _now():
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def record_command(
    user_id,
    *,
    transcript,
    interpreted,
    status,
    lead_id=None,
    result=None,
    confirmation_token=None,
    expires_at=None,
    model=None,
    input_source=None,
    tools_invoked=None,
    session_key=None,
):
    token_hash = hash_token(confirmation_token) if confirmation_token else None
    created = _now().isoformat()
    tools_json = json.dumps(tools_invoked or [])[:2000] if tools_invoked is not None else None
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO ask_topai_commands
                (user_id, source, transcript, interpreted_json, confirmation_token_hash,
                 status, lead_id, result_json, created_at, expires_at,
                 model, input_source, tools_invoked_json, session_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                SOURCE,
                str(transcript or "")[:4000],
                json.dumps(interpreted or {})[:4000],
                token_hash,
                status,
                lead_id,
                json.dumps(result or {})[:4000] if result is not None else None,
                created,
                expires_at,
                (str(model)[:80] if model else None),
                (str(input_source)[:20] if input_source else None),
                tools_json,
                (str(session_key)[:80] if session_key else None),
            ),
        )
        return cur.lastrowid


def get_by_token(user_id, token: str):
    if not token:
        return None
    token_hash = hash_token(token)
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM ask_topai_commands
            WHERE user_id = ? AND confirmation_token_hash = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, token_hash),
        ).fetchone()
    return dict(row) if row else None


def get_pending_by_token(user_id, token: str):
    if not token:
        return None
    token_hash = hash_token(token)
    now = _now().isoformat()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM ask_topai_commands
            WHERE user_id = ? AND confirmation_token_hash = ?
              AND status = 'pending_confirmation'
            """,
            (user_id, token_hash),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    expires = data.get("expires_at") or ""
    if expires and expires < now:
        mark_status(data["id"], user_id, "expired")
        return None
    return data


def mark_status(command_id, user_id, status, *, result=None, executed=False):
    from db_backend import bind_bool

    now = _now().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE ask_topai_commands
            SET status = ?,
                result_json = COALESCE(?, result_json),
                executed_at = CASE WHEN ? THEN ? ELSE executed_at END
            WHERE id = ? AND user_id = ?
            """,
            (
                status,
                json.dumps(result)[:4000] if result is not None else None,
                bind_bool(executed),
                now,
                command_id,
                user_id,
            ),
        )


def issue_confirmation_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = (_now() + timedelta(minutes=TOKEN_TTL_MINUTES)).isoformat()
    return token, expires


def add_lead_audit(user_id, lead_id, summary, payload):
    if not lead_id:
        return
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "ask_topai_command",
        summary,
        payload,
        actor_user_id=user_id,
    )


def list_recent(user_id, limit=20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, source, transcript, interpreted_json, status, lead_id,
                   result_json, created_at, executed_at, model, input_source,
                   tools_invoked_json, session_key
            FROM ask_topai_commands
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]
