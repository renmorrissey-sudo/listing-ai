"""Execute confirmed Phase 1 tools through existing CRM services."""

from __future__ import annotations

import json

import crm_db
import db
from ask_topai.audit import SOURCE, add_lead_audit
from ask_topai.schemas import build_property_interest
from external_leads.ingest import ingest_external_lead
from lead_service import duplicate_phone_message


def find_leads_by_name(user_id, name: str) -> list[dict]:
    query = (name or "").strip()
    if not query:
        return []
    leads = crm_db.filter_leads(user_id, search=query, limit=50)
    lowered = query.lower()
    exact = [lead for lead in leads if (lead.get("name") or "").strip().lower() == lowered]
    if exact:
        return exact
    return [
        lead
        for lead in leads
        if lowered in (lead.get("name") or "").lower()
    ]


def resolve_lead(user_id, arguments: dict, context: dict | None):
    """Return (lead, error_message, choices). Never guesses among multiple names."""
    context = context or {}
    lead_id = arguments.get("lead_id") or context.get("lead_id")
    lead_name = arguments.get("lead_name")
    named = bool(arguments.get("lead_name"))
    if named:
        matches = find_leads_by_name(user_id, lead_name)
        if not matches:
            return None, f"I could not find a lead named {lead_name}.", []
        if len(matches) > 1:
            choices = [
                {
                    "id": lead["id"],
                    "name": lead.get("name") or "Lead",
                    "phone_number": lead.get("phone_number"),
                }
                for lead in matches[:8]
            ]
            names = ", ".join(f"{c['name']} ({c['phone_number']})" for c in choices)
            return None, f"I found multiple leads named {lead_name}. Which one did you mean? {names}", choices
        return matches[0], None, []
    if lead_id:
        lead = db.get_lead(int(lead_id), user_id)
        if not lead:
            return None, "Lead not found.", []
        return lead, None, []
    return None, "Which lead should I use?", []


def _criteria_dict(lead) -> dict:
    raw = (lead or {}).get("property_criteria_json")
    if not raw:
        interest = (lead or {}).get("property_interest")
        return {"property_interest": interest} if interest else {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_property_criteria(existing: dict, incoming: dict, *, replace: bool) -> dict:
    base = {} if replace else dict(existing or {})
    for key, value in (incoming or {}).items():
        if key in {"lead_id", "lead_name", "replace"}:
            continue
        if value in (None, ""):
            continue
        base[key] = value
    return base


def _save_criteria(user_id, lead_id, merged: dict):
    interest = build_property_interest(merged) or merged.get("property_interest")
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET property_criteria_json = ?,
                property_interest = COALESCE(?, property_interest),
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (json.dumps(merged)[:2000], interest, stamp, lead_id, user_id),
        )
    db.update_lead_contact_fields(lead_id, user_id, property_interest=interest)


def append_note(user_id, lead_id, note: str):
    text = (note or "").strip()[:1500]
    if not text:
        return None, "Note text is required."
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET notes = CASE
                    WHEN notes IS NULL OR notes = '' THEN ?
                    ELSE notes || ' | ' || ?
                END,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (text, text, stamp, lead_id, user_id),
        )
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "note_added",
        "Note added",
        {"source": SOURCE, "note": text[:400]},
        actor_user_id=user_id,
    )
    return db.get_lead(lead_id, user_id), None


def execute_create_lead(user_id, arguments: dict):
    payload = {
        "full_name": arguments.get("name"),
        "phone": arguments.get("phone"),
        "email": arguments.get("email"),
        "lead_type": arguments.get("lead_type") or "buyer",
        "notes": arguments.get("notes") or arguments.get("desired_outcome"),
        "property_interest": arguments.get("property_interest"),
        "source": "ask_topai",
    }
    result = ingest_external_lead(
        user_id,
        payload,
        method="ask_topai",
        actor_user_id=user_id,
        allow_identity_update=False,
    )
    if result.get("error"):
        return None, result["error"], None
    if result.get("action") != "created":
        existing = db.get_lead(result.get("lead_id"), user_id) or {}
        return None, duplicate_phone_message(existing), existing
    lead = db.get_lead(result["lead_id"], user_id)
    incoming = {
        k: arguments.get(k)
        for k in (
            "price_min",
            "price_max",
            "bedrooms",
            "bathrooms",
            "city",
            "neighborhood",
            "property_type",
            "property_interest",
        )
        if arguments.get(k) not in (None, "")
    }
    if incoming:
        _save_criteria(user_id, lead["id"], incoming)
        lead = db.get_lead(lead["id"], user_id)
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI created lead {lead.get('name')}",
        {"action": "create_lead", "source": SOURCE},
    )
    return lead, None, None


def execute_add_note(user_id, arguments: dict, context: dict | None):
    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    updated, note_err = append_note(user_id, lead["id"], arguments.get("note"))
    if note_err:
        return None, note_err, None
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI added a note to {lead.get('name')}",
        {"action": "add_lead_note", "source": SOURCE},
    )
    return updated, None, None


def execute_create_task(user_id, arguments: dict, context: dict | None):
    lead = None
    if arguments.get("lead_id") or arguments.get("lead_name") or (context or {}).get("lead_id"):
        lead, err, choices = resolve_lead(user_id, arguments, context)
        if err:
            return None, err, choices
    payload = {
        "title": arguments.get("title"),
        "description": arguments.get("description") or "",
        "due_at": arguments.get("due_at"),
        "priority": arguments.get("priority") or "normal",
        "lead_id": lead["id"] if lead else None,
    }
    task_id, error = crm_db.create_task(user_id, payload)
    if error:
        return None, error, None
    task = crm_db.get_task(user_id, task_id)
    if lead:
        add_lead_audit(
            user_id,
            lead["id"],
            f"Ask TopAI created task: {arguments.get('title')}",
            {"action": "create_task", "task_id": task_id, "source": SOURCE},
        )
    return task, None, None


def execute_update_criteria(user_id, arguments: dict, context: dict | None):
    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    merged = merge_property_criteria(
        _criteria_dict(lead),
        arguments,
        replace=bool(arguments.get("replace")),
    )
    _save_criteria(user_id, lead["id"], merged)
    updated = db.get_lead(lead["id"], user_id)
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI updated property criteria for {lead.get('name')}",
        {"action": "update_property_criteria", "source": SOURCE},
    )
    return updated, None, None


def execute_command(user_id, command: dict, context: dict | None):
    action = command.get("action")
    arguments = command.get("arguments") or {}
    if action == "create_lead":
        return "create_lead", *execute_create_lead(user_id, arguments)
    if action == "add_lead_note":
        return "add_lead_note", *execute_add_note(user_id, arguments, context)
    if action == "create_task":
        return "create_task", *execute_create_task(user_id, arguments, context)
    if action == "update_property_criteria":
        return "update_property_criteria", *execute_update_criteria(user_id, arguments, context)
    return action, None, "That command is not allowed.", None


def success_message(action: str, payload) -> str:
    if action == "create_lead":
        return f"Lead created: {(payload or {}).get('name') or 'Lead'}"
    if action == "add_lead_note":
        return f"Note added to {(payload or {}).get('name') or 'lead'}"
    if action == "create_task":
        title = (payload or {}).get("title") or "Task"
        due = (payload or {}).get("due_at")
        if due:
            return f"Task created: {title} — due {str(due)[:16].replace('T', ' ')}"
        return f"Task created: {title}"
    if action == "update_property_criteria":
        return f"Property criteria updated for {(payload or {}).get('name') or 'lead'}"
    return "Done."
