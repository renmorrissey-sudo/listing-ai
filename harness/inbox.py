"""Harness inbox — READ-ONLY SMS view.

Wraps the existing helpers (:func:`db.list_lead_messages` and
:func:`db.list_sms_messages` / :func:`db.get_lead`) so the harness never
invents its own SMS queries. Every query is ``user_id``-scoped; no writes,
no sends, no status changes happen here.
"""

from __future__ import annotations

_PREVIEW_CHARS = 120


def list_sms_conversations(user_id, limit=50):
    """One row per lead with SMS history for *user_id*.

    Returns a list of ``{lead_id, lead_name, lead_phone, message_count,
    last_message_at, last_message_preview}`` ordered by most recent message
    first. A conversation is a lead with at least one row in
    ``sms_messages``; leads without messages do not appear.
    """
    if user_id is None:
        return []
    import db

    fetch_limit = max(int(limit or 50), 1)
    with db.get_db() as conn:
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
        conversations = [dict(row) for row in rows]

    result = []
    for conv in conversations:
        lead = db.get_lead(conv["lead_id"], user_id)
        if not lead:
            continue
        preview = ""
        try:
            latest = db.list_lead_messages(user_id, conv["lead_id"], limit=1)
            if latest:
                body = str(latest[-1].get("message_body") or latest[-1].get("body") or "")
                preview = body[:_PREVIEW_CHARS]
        except Exception:
            preview = ""
        result.append(
            {
                "lead_id": conv["lead_id"],
                "lead_name": lead.get("name"),
                "lead_phone": lead.get("phone_number"),
                "message_count": int(conv["message_count"] or 0),
                "last_message_at": conv["last_message_at"],
                "last_message_preview": preview,
            }
        )
    return result


def get_conversation(user_id, lead_id, limit=100):
    """Full SMS thread for one lead, plus a lead summary.

    Returns ``{"lead": {...}, "messages": [...]}`` or None when the lead
    does not belong to *user_id*. Messages come from
    :func:`db.list_lead_messages` (oldest first); no writes are performed.
    """
    if user_id is None or lead_id is None:
        return None
    import db

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None
    messages = db.list_lead_messages(user_id, lead["id"], limit=limit)
    return {
        "lead": {
            "lead_id": lead["id"],
            "name": lead.get("name"),
            "status": lead.get("status"),
            "phone_number": lead.get("phone_number"),
            "email": lead.get("email"),
            "sms_consent_status": lead.get("sms_consent_status"),
        },
        "messages": messages,
    }
