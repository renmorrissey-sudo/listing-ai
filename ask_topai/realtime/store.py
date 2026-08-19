"""Persist live-session metadata and idempotent tool invocations. No secrets. No audio."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from db import get_db

from ask_topai import sessions

LIVE_STATUS = "live"


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_state(user_id, session_key: str | None) -> dict:
    row = sessions.get_session(user_id, session_key)
    if not row:
        return {}
    try:
        pending = json.loads(row.get("pending_json") or "{}")
    except json.JSONDecodeError:
        pending = {}
    return pending if isinstance(pending, dict) else {}


def save_state(user_id, session_key: str, state: dict, *, messages=None, status=LIVE_STATUS):
    existing_messages = messages
    if existing_messages is None:
        existing_messages = sessions.load_messages(user_id, session_key)
    return sessions.save_session(
        user_id,
        session_key,
        existing_messages,
        pending=state or {},
        status=status,
    )


def completed_actions(state: dict) -> list:
    items = (state or {}).get("completed_actions") or []
    return items if isinstance(items, list) else []


def remember_action(state: dict, item: dict) -> dict:
    state = dict(state or {})
    actions = list(completed_actions(state))
    actions.append(item)
    state["completed_actions"] = actions[-40:]
    if item.get("lead_id") and item.get("tool") == "create_lead":
        state["last_created_lead"] = {
            "id": item.get("lead_id"),
            "name": item.get("lead_name"),
        }
    return state


def last_created_lead(state: dict) -> dict | None:
    lead = (state or {}).get("last_created_lead")
    return lead if isinstance(lead, dict) else None


def get_invocation(user_id, session_key: str, *, call_id=None, action_id=None):
    if not session_key:
        return None
    with get_db() as conn:
        if call_id:
            row = conn.execute(
                """
                SELECT * FROM ask_topai_tool_invocations
                WHERE user_id = ? AND session_key = ? AND call_id = ?
                """,
                (user_id, session_key, str(call_id)[:80]),
            ).fetchone()
            if row:
                return dict(row)
        if action_id:
            row = conn.execute(
                """
                SELECT * FROM ask_topai_tool_invocations
                WHERE user_id = ? AND session_key = ? AND action_id = ?
                """,
                (user_id, session_key, str(action_id)[:80]),
            ).fetchone()
            if row:
                return dict(row)
    return None


def _is_unique_violation(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    pgcode = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if str(pgcode) == "23505":
        return True
    text = str(exc or "").lower()
    return "unique" in text and "ask_topai_tool" in text


def claim_invocation(user_id, session_key, *, call_id, action_id, tool_name, arguments):
    """Insert an in-progress row. Return existing row if this action already ran."""
    existing = get_invocation(user_id, session_key, call_id=call_id, action_id=action_id)
    if existing:
        return existing, False
    now = _now()
    with get_db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO ask_topai_tool_invocations
                    (user_id, session_key, call_id, action_id, tool_name,
                     arguments_json, result_json, status, lead_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_key,
                    str(call_id)[:80],
                    str(action_id)[:80],
                    str(tool_name)[:80],
                    json.dumps(arguments or {})[:4000],
                    json.dumps({"status": "in_progress"}),
                    "in_progress",
                    None,
                    now,
                ),
            )
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            existing = get_invocation(user_id, session_key, call_id=call_id, action_id=action_id)
            return existing, False
    return get_invocation(user_id, session_key, call_id=call_id), True


def complete_invocation(user_id, session_key, call_id, *, result, status, lead_id=None):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE ask_topai_tool_invocations
            SET result_json = ?, status = ?, lead_id = COALESCE(?, lead_id)
            WHERE user_id = ? AND session_key = ? AND call_id = ?
            """,
            (
                json.dumps(result or {})[:4000],
                status,
                lead_id,
                user_id,
                session_key,
                str(call_id)[:80],
            ),
        )
    return get_invocation(user_id, session_key, call_id=call_id)


def record_invocation(
    user_id,
    session_key: str,
    *,
    call_id: str,
    action_id: str,
    tool_name: str,
    arguments: dict,
    result: dict,
    status: str,
    lead_id=None,
):
    now = _now()
    payload_args = json.dumps(arguments or {})[:4000]
    payload_result = json.dumps(result or {})[:4000]
    with get_db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO ask_topai_tool_invocations
                    (user_id, session_key, call_id, action_id, tool_name,
                     arguments_json, result_json, status, lead_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_key,
                    str(call_id)[:80],
                    str(action_id)[:80],
                    str(tool_name)[:80],
                    payload_args,
                    payload_result,
                    status,
                    lead_id,
                    now,
                ),
            )
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            conn.execute(
                """
                UPDATE ask_topai_tool_invocations
                SET result_json = ?, status = ?, lead_id = COALESCE(?, lead_id)
                WHERE user_id = ? AND session_key = ?
                  AND (call_id = ? OR action_id = ?)
                """,
                (
                    payload_result,
                    status,
                    lead_id,
                    user_id,
                    session_key,
                    str(call_id)[:80],
                    str(action_id)[:80],
                ),
            )
    return get_invocation(user_id, session_key, call_id=call_id, action_id=action_id)


def invocation_output(row: dict | None) -> dict | None:
    if not row:
        return None
    try:
        payload = json.loads(row.get("result_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}
