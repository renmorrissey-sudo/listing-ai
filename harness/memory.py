"""Harness memory — thin wrapper over existing stores.

* Structured per-tenant memory lives in the ``harness_memory`` table
  (created by migration 029; until it lands, reads return ``None`` and
  writes are a logged no-op rather than raising).
* The business profile is read from the existing ``users`` columns via
  :func:`db.get_business_profile` — no new copy of it is kept here.
* Lead summaries are built from the existing CRM tables (leads, tasks,
  follow-ups, activities). Summaries only — raw transcripts / message
  bodies are never stored or returned here.

Every query is ``user_id``-scoped. ``?`` placeholders are used throughout
(``db_backend.CompatConnection`` translates them for Postgres).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def get_memory(user_id, key):
    """Return the stored JSON value for ``(user_id, key)``, or None.

    Returns None when the key is absent *and* when the ``harness_memory``
    table does not exist yet (migration pending).
    """
    if user_id is None or not key:
        return None
    try:
        import db

        with db.get_db() as conn:
            row = conn.execute(
                "SELECT value_json FROM harness_memory WHERE user_id = ? AND key = ?",
                (user_id, str(key)),
            ).fetchone()
    except Exception as exc:  # table missing until migration 029 lands
        logger.warning("harness get_memory unavailable (table missing?): %s", exc)
        return None
    if not row:
        return None
    raw = row["value_json"]
    if raw is None:
        return None
    if isinstance(raw, (dict, list, bool, int, float)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def set_memory(user_id, key, value, source="harness") -> bool:
    """Store ``value`` (JSON-serialised) for ``(user_id, key)``.

    Returns True when stored, False when the ``harness_memory`` table does
    not exist yet — in that case the write is a logged no-op (not an
    exception) until the migration lands.
    """
    if user_id is None or not key:
        return False
    try:
        import db
        from datetime import datetime, timezone

        payload = json.dumps(value)
        now = datetime.now(timezone.utc).isoformat()
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM harness_memory WHERE user_id = ? AND key = ?",
                (user_id, str(key)),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE harness_memory SET value_json = ?, source = ?, updated_at = ? "
                    "WHERE user_id = ? AND key = ?",
                    (payload, source, now, user_id, str(key)),
                )
            else:
                conn.execute(
                    "INSERT INTO harness_memory (user_id, key, value_json, source, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, str(key), payload, source, now),
                )
        return True
    except Exception as exc:  # table missing until migration 029 lands
        logger.warning("harness set_memory skipped (table missing?): %s", exc)
        return False


def load_profile(user_id):
    """Business profile for *user_id* from the ``users`` table columns.

    Returns a dict with ``agent_name``, ``phone_number``,
    ``brokerage_name``, ``company_name``, ``timezone`` (plus the account
    ``email`` when available), or ``{}`` when the user does not exist.
    """
    if user_id is None:
        return {}
    import db

    profile = db.get_business_profile(user_id) or {}
    user = db.get_user_by_id(user_id) or {}
    if user.get("email"):
        profile["email"] = user["email"]
    return profile


def lead_summary(user_id, lead_id):
    """Summary-only snapshot of one lead for harness context.

    Includes identity/status, property interest, open-task count and titles,
    upcoming follow-ups, and recent activity *summaries*. Never includes raw
    SMS/email transcripts or message bodies. Returns None when the lead
    does not belong to *user_id*.
    """
    if user_id is None or lead_id is None:
        return None
    import crm_db
    import db

    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None

    tasks = []
    try:
        for task in crm_db.list_tasks(user_id, limit=200) or []:
            if task.get("lead_id") != lead["id"]:
                continue
            if task.get("status") not in {"open", "in_progress"}:
                continue
            tasks.append(
                {
                    "id": task.get("id"),
                    "title": task.get("title"),
                    "due_at": task.get("due_at"),
                    "status": task.get("status"),
                }
            )
    except Exception as exc:
        logger.warning("harness lead_summary tasks unavailable: %s", exc)

    follow_ups = []
    try:
        for item in crm_db.list_lead_follow_ups(user_id, lead["id"], limit=20) or []:
            if not item.get("is_open", item.get("status") in {"pending", "open"}):
                continue
            follow_ups.append(
                {
                    "id": item.get("id"),
                    "due_at": item.get("due_at"),
                    "reason": item.get("reason"),
                    "priority": item.get("priority"),
                }
            )
    except Exception as exc:
        logger.warning("harness lead_summary follow-ups unavailable: %s", exc)

    activities = []
    try:
        for item in crm_db.list_lead_activities(user_id, lead["id"], limit=10) or []:
            # Summaries only: event type + summary line, never payload bodies.
            activities.append(
                {
                    "event_type": item.get("event_type"),
                    "summary": item.get("summary"),
                    "created_at": item.get("created_at"),
                }
            )
    except Exception as exc:
        logger.warning("harness lead_summary activities unavailable: %s", exc)

    return {
        "lead_id": lead["id"],
        "name": lead.get("name"),
        "status": lead.get("status"),
        "lead_type": lead.get("lead_type"),
        "phone_number": lead.get("phone_number"),
        "email": lead.get("email"),
        "property_interest": lead.get("property_interest"),
        "next_action": lead.get("next_action"),
        "next_follow_up_at": lead.get("next_follow_up_at"),
        "sms_consent_status": lead.get("sms_consent_status"),
        "open_tasks": tasks,
        "open_task_count": len(tasks),
        "upcoming_follow_ups": follow_ups,
        "recent_activity": activities,
    }
