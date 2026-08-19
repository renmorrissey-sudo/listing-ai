"""Ask TopAI conversation sessions. Tenant-scoped, short-lived, no secrets."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from db import get_db

SESSION_TTL_MINUTES = 30
MAX_MESSAGES = 16
MAX_JSON = 12000


def _now():
    return datetime.now(timezone.utc)


def create_session_key() -> str:
    return secrets.token_urlsafe(18)


def get_session(user_id, session_key: str | None):
    if not session_key:
        return None
    now = _now().isoformat()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM ask_topai_sessions
            WHERE user_id = ? AND session_key = ?
            """,
            (user_id, session_key),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    if (data.get("expires_at") or "") < now:
        delete_session(user_id, session_key)
        return None
    return data


def load_messages(user_id, session_key: str | None) -> list:
    session = get_session(user_id, session_key)
    if not session:
        return []
    try:
        messages = json.loads(session.get("messages_json") or "[]")
    except json.JSONDecodeError:
        return []
    return messages if isinstance(messages, list) else []


def save_session(user_id, session_key: str, messages: list, *, pending=None, status="active"):
    key = (session_key or "").strip() or create_session_key()
    expires = (_now() + timedelta(minutes=SESSION_TTL_MINUTES)).isoformat()
    now = _now().isoformat()
    trimmed = list(messages or [])[-MAX_MESSAGES:]
    payload = json.dumps(trimmed)[:MAX_JSON]
    pending_json = json.dumps(pending)[:12000] if pending is not None else None
    existing = get_session(user_id, key)
    with get_db() as conn:
        if existing:
            conn.execute(
                """
                UPDATE ask_topai_sessions
                SET messages_json = ?, pending_json = ?, status = ?,
                    updated_at = ?, expires_at = ?
                WHERE user_id = ? AND session_key = ?
                """,
                (payload, pending_json, status, now, expires, user_id, key),
            )
        else:
            conn.execute(
                """
                INSERT INTO ask_topai_sessions
                    (user_id, session_key, messages_json, pending_json, status,
                     created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, key, payload, pending_json, status, now, now, expires),
            )
    return key


def delete_session(user_id, session_key: str):
    if not session_key:
        return
    with get_db() as conn:
        conn.execute(
            "DELETE FROM ask_topai_sessions WHERE user_id = ? AND session_key = ?",
            (user_id, session_key),
        )


def conversation_transcript(messages: list) -> str:
    parts = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role == "user" and isinstance(content, str):
            parts.append(content)
        elif role == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
    return "\n".join(p for p in parts if p).strip()
