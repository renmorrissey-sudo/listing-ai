"""Harness inbox — SMS conversations, unread state, human takeover.

Wraps the existing helpers (:func:`db.list_lead_messages` and
:func:`db.list_sms_messages` / :func:`db.get_lead`) so the harness never
invents its own SMS queries. Every query is ``user_id``-scoped; a tenant
can only ever see their own leads, messages, and conversation state.

A *conversation* is one lead's SMS thread (a lead with at least one row
in ``sms_messages``). Per-conversation state lives in
``harness_conversations`` (migration 042):

* ``human_takeover`` — a human has taken over this conversation. The
  policy gate already honours ``human_takeover`` in its context
  (``harness/policy.py`` step 5: DRAFT "a human has taken over this
  conversation"); :func:`is_human_takeover` is the reader the inbound
  auto-reply gate uses, and :func:`set_human_takeover` is the setter
  the inbox UI calls (audited).
* ``last_read_at`` — when the signed-in tenant last opened this
  conversation. Unread = inbound messages newer than ``last_read_at``
  (or all inbound when it is NULL). Drives the unread badge and the
  unread filter.

Approval display: the inbox renders pending approvals through
``harness.approvals.serialize_approval``, which reads the draft text
from the approval row's stored ``draft_json`` (``draft_reply``) verbatim.
The inbox never re-derives, re-generates, or caches approval text from
any other source — the stored draft row is the single source of truth,
so every surface shows one consistent version.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from db_backend import bind_bool

logger = logging.getLogger(__name__)

_PREVIEW_CHARS = 120


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return dict(row) if row is not None else None


def _unread_counts(user_id):
    """Inbound-message counts newer than each conversation's last read.

    Returns ``{lead_id: unread_count}`` for *user_id*. A conversation
    whose ``last_read_at`` is NULL counts all its inbound messages as
    unread. One query for every conversation; never raises (returns {}
    on error so the inbox list still renders).
    """
    if user_id is None:
        return {}
    import db

    try:
        with db.get_db() as conn:
            rows = conn.execute(
                """
                SELECT sm.lead_id AS lead_id, COUNT(*) AS unread_count
                FROM sms_messages sm
                LEFT JOIN harness_conversations hc
                  ON hc.user_id = sm.user_id AND hc.lead_id = sm.lead_id
                WHERE sm.user_id = ? AND sm.lead_id IS NOT NULL
                  AND sm.direction = 'inbound'
                  AND (hc.last_read_at IS NULL
                       OR sm.created_at > hc.last_read_at)
                GROUP BY sm.lead_id
                """,
                (user_id,),
            ).fetchall()
    except Exception:
        logger.warning("inbox unread-count read failed tenant=%s", user_id)
        return {}
    return {int(r["lead_id"]): int(r["unread_count"] or 0) for r in rows}


def _conversation_states(user_id):
    """All ``harness_conversations`` rows for *user_id*, keyed by lead.

    Returns ``{lead_id: {human_takeover, last_read_at, ...}}``. Never
    raises (returns {} on error).
    """
    if user_id is None:
        return {}
    import db

    try:
        with db.get_db() as conn:
            rows = conn.execute(
                """
                SELECT lead_id, human_takeover, last_read_at,
                       created_at, updated_at
                FROM harness_conversations
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()
    except Exception:
        logger.warning("inbox conversation-state read failed tenant=%s", user_id)
        return {}
    states = {}
    for r in rows:
        d = _row(r)
        states[int(d["lead_id"])] = {
            "lead_id": int(d["lead_id"]),
            "human_takeover": bool(d.get("human_takeover")),
            "last_read_at": d.get("last_read_at"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
        }
    return states


def list_sms_conversations(user_id, limit=50, *, query=None, unread_only=False):
    """One row per lead with SMS history for *user_id*.

    Returns a list of ``{lead_id, lead_name, lead_phone, message_count,
    last_message_at, last_message_preview, unread_count, human_takeover}``
    ordered by most recent message first. A conversation is a lead with
    at least one row in ``sms_messages``; leads without messages do not
    appear.

    ``query`` filters by lead name or phone (case-insensitive substring
    match, bound as a parameter — never interpolated into the SQL).
    ``unread_only`` keeps only conversations with unread inbound
    messages. Both filters apply to the signed-in tenant's data only.
    """
    if user_id is None:
        return []
    import db

    fetch_limit = max(int(limit or 50), 1)
    search = (query or "").strip()
    like = f"%{search}%" if search else None
    with db.get_db() as conn:
        if like is None:
            rows = conn.execute(
                """
                SELECT lead_id,
                       COUNT(*) AS message_count,
                       MAX(created_at) AS last_message_at
                FROM sms_messages
                WHERE user_id = ? AND lead_id IS NOT NULL
                GROUP BY lead_id
                ORDER BY last_message_at DESC, lead_id DESC
                LIMIT ?
                """,
                (user_id, fetch_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT sm.lead_id AS lead_id,
                       COUNT(*) AS message_count,
                       MAX(sm.created_at) AS last_message_at
                FROM sms_messages sm
                JOIN leads l ON l.id = sm.lead_id AND l.user_id = sm.user_id
                WHERE sm.user_id = ? AND sm.lead_id IS NOT NULL
                  AND (l.name LIKE ? OR l.phone_number LIKE ?)
                GROUP BY sm.lead_id
                ORDER BY last_message_at DESC, sm.lead_id DESC
                LIMIT ?
                """,
                (user_id, like, like, fetch_limit),
            ).fetchall()
        conversations = [dict(row) for row in rows]

    unread = _unread_counts(user_id)
    states = _conversation_states(user_id)
    result = []
    for conv in conversations:
        lead = db.get_lead(conv["lead_id"], user_id)
        if not lead:
            continue
        lead_id = int(conv["lead_id"])
        unread_count = int(unread.get(lead_id, 0))
        if unread_only and unread_count <= 0:
            continue
        preview = ""
        try:
            latest = db.list_lead_messages(user_id, lead_id, limit=1)
            if latest:
                body = str(latest[-1].get("message_body") or latest[-1].get("body") or "")
                preview = body[:_PREVIEW_CHARS]
        except Exception:
            preview = ""
        state = states.get(lead_id) or {}
        result.append(
            {
                "lead_id": lead_id,
                "lead_name": lead.get("name"),
                "lead_phone": lead.get("phone_number"),
                "message_count": int(conv["message_count"] or 0),
                "last_message_at": conv["last_message_at"],
                "last_message_preview": preview,
                "unread_count": unread_count,
                "human_takeover": bool(state.get("human_takeover", False)),
            }
        )
    return result


def get_conversation(user_id, lead_id, limit=100):
    """Full SMS thread for one lead, plus a lead summary.

    Returns ``{"lead": {...}, "messages": [...]}`` or None when the lead
    does not belong to *user_id*. Messages come from
    :func:`db.list_lead_messages` (oldest first); no writes are performed.
    The lead summary carries the conversation's ``human_takeover`` flag
    and ``unread_count``.
    """
    if user_id is None or lead_id is None:
        return None
    import db

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None
    messages = db.list_lead_messages(user_id, lead["id"], limit=limit)
    states = _conversation_states(user_id)
    state = states.get(int(lead["id"])) or {}
    unread = _unread_counts(user_id)
    return {
        "lead": {
            "lead_id": lead["id"],
            "name": lead.get("name"),
            "status": lead.get("status"),
            "phone_number": lead.get("phone_number"),
            "email": lead.get("email"),
            "sms_consent_status": lead.get("sms_consent_status"),
            "opt_out_status": lead.get("opt_out_status"),
            "human_takeover": bool(state.get("human_takeover", False)),
            "unread_count": int(unread.get(int(lead["id"]), 0)),
            "last_read_at": state.get("last_read_at"),
        },
        "messages": messages,
    }


def get_conversation_state(user_id, lead_id):
    """One ``harness_conversations`` row, user_id-scoped, or None.

    Returns None when the row does not exist (no takeover, never read)
    or when the read fails. Never raises.
    """
    if user_id is None or lead_id is None:
        return None
    states = _conversation_states(user_id)
    return states.get(int(lead_id))


def is_human_takeover(user_id, lead_id):
    """True when a human has taken over this lead's conversation.

    The gate reader (``harness.sms_handler._decide_policy``). Never
    raises — a failed read returns False so normal gating still applies
    (the gate itself defaults to DRAFT on any policy error).
    """
    try:
        state = get_conversation_state(user_id, lead_id)
    except Exception:
        return False
    return bool((state or {}).get("human_takeover", False))


def _upsert_state(conn, user_id, lead_id, *, human_takeover=None, last_read_at=None):
    """Insert or update one ``harness_conversations`` row in *conn*.

    Only the supplied fields change; the row is created on first use.
    Returns the stored state dict. Caller owns the transaction.
    """
    now = _now_iso()
    row = conn.execute(
        "SELECT id, human_takeover, last_read_at FROM harness_conversations "
        "WHERE user_id = ? AND lead_id = ?",
        (user_id, lead_id),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO harness_conversations
                (user_id, lead_id, human_takeover, last_read_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                lead_id,
                bind_bool(human_takeover),
                last_read_at,
                now,
                now,
            ),
        )
        return {
            "lead_id": int(lead_id),
            "human_takeover": bool(human_takeover),
            "last_read_at": last_read_at,
            "created_at": now,
            "updated_at": now,
        }
    d = _row(row)
    new_takeover = (
        bool(d.get("human_takeover"))
        if human_takeover is None
        else bool(human_takeover)
    )
    new_read = d.get("last_read_at") if last_read_at is None else last_read_at
    conn.execute(
        """
        UPDATE harness_conversations
        SET human_takeover = ?, last_read_at = ?, updated_at = ?
        WHERE user_id = ? AND lead_id = ?
        """,
        (bind_bool(new_takeover), new_read, now, user_id, lead_id),
    )
    return {
        "lead_id": int(lead_id),
        "human_takeover": new_takeover,
        "last_read_at": new_read,
        "created_at": d.get("created_at"),
        "updated_at": now,
    }


def set_human_takeover(user_id, lead_id, active, *, actor="user"):
    """Set or clear the human-takeover flag for one conversation.

    The lead must belong to *user_id*; otherwise returns None (the
    caller maps that to 404 and never reveals the lead exists). Every
    change writes one ``harness_audit`` row (``harness_takeover_set`` /
    ``harness_takeover_cleared``) so takeover is auditable. Returns the
    stored state dict.
    """
    if user_id is None or lead_id is None:
        return None
    import db

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None
    active = bool(active)
    with db.get_db() as conn:
        state = _upsert_state(
            conn, user_id, int(lead["id"]), human_takeover=active
        )
    try:
        from harness.audit import record_audit

        record_audit(
            user_id,
            actor,
            "harness_takeover_set" if active else "harness_takeover_cleared",
            tool="harness_inbox",
            target_type="conversation",
            target_id=int(lead["id"]),
            gate_reason=(
                "human took over this conversation; "
                "the gate will draft instead of auto-sending"
                if active
                else "human takeover cleared; normal gating resumes"
            ),
        )
    except Exception:
        logger.warning(
            "inbox takeover audit write failed tenant=%s lead=%s",
            user_id,
            lead_id,
        )
    return state


def mark_conversation_read(user_id, lead_id):
    """Mark a conversation read (set ``last_read_at`` to now).

    The lead must belong to *user_id*; otherwise returns None. Read
    markers are not audited (they would flood the audit trail). Returns
    the stored state dict.
    """
    if user_id is None or lead_id is None:
        return None
    import db

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None
    with db.get_db() as conn:
        return _upsert_state(
            conn, user_id, int(lead["id"]), last_read_at=_now_iso()
        )
