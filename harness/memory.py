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


def build_recall_context(user_id, lead_id):
    """Per-lead recall block for the coach prompt (Phase III-5).

    Combines the business profile, the lead summary (identity/status,
    property interest, open tasks, upcoming follow-ups, recent activity
    *summaries*) and any KV preferences (tenant-level ``preferences``
    plus ``lead_preferences:<lead_id>``).

    READ-ONLY by construction: pure reads, no lead write, no status
    or score change. Summaries only — raw message bodies never
    appear (``lead_summary`` never includes them).

    Returns a plain-text block ("" when the lead does not belong to
    *user_id*).
    """
    summary = lead_summary(user_id, lead_id)
    if not summary:
        return ""

    profile = load_profile(user_id) or {}
    tenant_prefs = get_memory(user_id, "preferences")
    lead_prefs = get_memory(user_id, f"lead_preferences:{lead_id}")

    lines = [f"Lead: {summary.get('name') or 'unknown'}"]
    status = summary.get("status")
    if status:
        lines[-1] += f" (status: {status})"
    if summary.get("lead_type"):
        lines[-1] += f", type: {summary['lead_type']}"
    if summary.get("property_interest"):
        lines.append(f"Property interest: {summary['property_interest']}")
    if summary.get("next_action"):
        lines.append(f"Next action: {summary['next_action']}")
    if summary.get("next_follow_up_at"):
        lines.append(f"Next follow-up: {summary['next_follow_up_at']}")

    tasks = summary.get("open_tasks") or []
    if tasks:
        titles = "; ".join(
            str(t.get("title") or "").strip() for t in tasks
            if str(t.get("title") or "").strip()
        )
        if titles:
            lines.append(f"Open tasks ({len(tasks)}): {titles}")

    follow_ups = summary.get("upcoming_follow_ups") or []
    if follow_ups:
        reasons = "; ".join(
            str(f.get("reason") or "").strip() for f in follow_ups
            if str(f.get("reason") or "").strip()
        )
        if reasons:
            lines.append(f"Upcoming follow-ups: {reasons}")

    activities = summary.get("recent_activity") or []
    if activities:
        entries = "; ".join(
            f"{a.get('event_type')}: {a.get('summary')}"
            for a in activities
            if str(a.get("summary") or "").strip()
        )
        if entries:
            lines.append(f"Recent activity: {entries}")

    agent_name = (profile.get("agent_name") or "").strip()
    brokerage = (profile.get("brokerage_name") or "").strip()
    if agent_name or brokerage:
        who = " / ".join(part for part in (agent_name, brokerage) if part)
        lines.append(f"Agent: {who}")

    def _prefs_text(value):
        if isinstance(value, dict):
            parts = [
                f"{key}: {val}"
                for key, val in value.items()
                if str(val or "").strip()
            ]
            return "; ".join(parts)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    prefs = _prefs_text(tenant_prefs)
    if prefs:
        lines.append(f"Preferences: {prefs}")
    lead_prefs_text = _prefs_text(lead_prefs)
    if lead_prefs_text:
        lines.append(f"Lead preferences: {lead_prefs_text}")

    return "\n".join(lines)
