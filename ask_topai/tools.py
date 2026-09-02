"""Execute Ask TopAI tools through existing CRM services. Writes are never run here."""

from __future__ import annotations

import json

import crm_db
import db
from ask_topai import actions, registry
from ask_topai.schemas import alias_location_fields, preview_rows, sanitize_command
from lead_service import format_phone_display, normalize_phone_e164


def _public_lead(lead: dict) -> dict:
    return {
        "id": lead.get("id"),
        "name": lead.get("name") or "Lead",
        "phone": format_phone_display(lead.get("phone_number") or ""),
        "email": lead.get("email") or None,
        "status": lead.get("status"),
        "lead_type": lead.get("lead_type"),
    }


def find_lead(user_id, arguments: dict, _context: dict | None = None) -> dict:
    name = str(arguments.get("name") or "").strip()
    phone = str(arguments.get("phone") or "").strip()
    email = str(arguments.get("email") or "").strip().lower()
    matches = []
    seen = set()

    def add(lead):
        if not lead or lead.get("id") in seen:
            return
        seen.add(lead["id"])
        matches.append(_public_lead(lead))

    if phone:
        add(db.find_lead_by_phone_normalized(user_id, normalize_phone_e164(phone) or phone))
    query = name or email or phone
    if query:
        for lead in crm_db.filter_leads(user_id, search=query, limit=20):
            if email:
                lead_email = (lead.get("email") or "").strip().lower()
                if lead_email and email not in lead_email and lead_email not in email:
                    if not name or name.lower() not in (lead.get("name") or "").lower():
                        continue
            add(lead)
    return {"matches": matches[:8], "count": len(matches[:8])}


def get_lead_context(user_id, arguments: dict, _context: dict | None = None) -> dict:
    try:
        lead_id = int(arguments.get("lead_id"))
    except (TypeError, ValueError):
        return {"error": "A valid lead_id is required."}
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return {"error": "Lead not found."}
    criteria = {}
    raw = lead.get("property_criteria_json")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                criteria = parsed
        except (TypeError, ValueError):
            criteria = {}
    consent = (lead.get("sms_consent_status") or "").strip().lower()
    blocked = bool(lead.get("sms_sending_blocked"))
    if consent in {"verified", "user_certified"} and not blocked:
        sms_status = "eligible"
    elif consent == "opted_out" or blocked:
        sms_status = "blocked"
    else:
        sms_status = "unverified"
    tasks = [
        {
            "id": task.get("id"),
            "title": task.get("title"),
            "due_at": task.get("due_at"),
            "status": task.get("status"),
        }
        for task in crm_db.list_tasks(user_id, limit=50)
        if task.get("lead_id") == lead_id and (task.get("status") or "open") != "done"
    ][:8]
    follow_ups = []
    try:
        for item in crm_db.list_follow_ups(user_id, limit=50)[:40]:
            if item.get("lead_id") == lead_id and (item.get("status") or "") not in {"completed", "cancelled", "canceled"}:
                follow_ups.append(
                    {
                        "id": item.get("id"),
                        "title": item.get("title") or item.get("summary") or "Follow-up",
                        "due_at": item.get("due_at"),
                        "status": item.get("status"),
                    }
                )
            if len(follow_ups) >= 8:
                break
    except Exception:
        follow_ups = []
    notes = (lead.get("notes") or "")[:800]
    return {
        "id": lead["id"],
        "name": lead.get("name") or "Lead",
        "status": lead.get("status"),
        "lead_type": lead.get("lead_type"),
        "phone": format_phone_display(lead.get("phone_number") or ""),
        "email": lead.get("email") or None,
        "property_interest": lead.get("property_interest"),
        "property_criteria": criteria,
        "notes": notes or None,
        "sms_qualification": sms_status,
        "upcoming_tasks": tasks,
        "upcoming_follow_ups": follow_ups,
    }


def list_lead_tasks(user_id, arguments: dict, _context: dict | None = None) -> dict:
    try:
        lead_id = int(arguments.get("lead_id"))
    except (TypeError, ValueError):
        return {"error": "A valid lead_id is required."}
    if not db.get_lead(lead_id, user_id):
        return {"error": "Lead not found."}
    tasks = [
        {
            "id": task.get("id"),
            "title": task.get("title"),
            "description": (task.get("description") or "")[:300] or None,
            "due_at": task.get("due_at"),
            "priority": task.get("priority"),
            "status": task.get("status"),
        }
        for task in crm_db.list_tasks(user_id, limit=80)
        if task.get("lead_id") == lead_id
    ]
    return {"lead_id": lead_id, "tasks": tasks[:20]}


def list_open_leads(user_id, arguments: dict, _context: dict | None = None) -> dict:
    """Return the current tenant's active lead count and a bounded lead list."""
    try:
        limit = max(0, min(int(arguments.get("limit", 20)), 100))
    except (TypeError, ValueError):
        limit = 20
    leads = crm_db.filter_leads(user_id, scope="active", limit=limit)
    return {
        "count": crm_db.count_filtered_leads(user_id, scope="active"),
        "leads": [_public_lead(lead) for lead in leads],
        "definition": "Leads not marked closed won, closed lost, or do not contact.",
    }


def get_calendar_availability(user_id, arguments: dict, _context: dict | None = None) -> dict:
    import scheduling

    return scheduling.get_calendar_availability(
        user_id,
        start_at=arguments.get("start_at"),
        end_at=arguments.get("end_at"),
        date=arguments.get("date"),
    )


def find_available_slots(user_id, arguments: dict, _context: dict | None = None) -> dict:
    import scheduling

    slots = scheduling.find_available_slots(
        user_id,
        after=arguments.get("after"),
        before=arguments.get("before"),
        duration_minutes=arguments.get("duration_minutes"),
        limit=8,
    )
    return {"slots": slots, "count": len(slots)}


def get_existing_appointment(user_id, arguments: dict, context: dict | None = None) -> dict:
    import scheduling

    lead = None
    lead_id = arguments.get("lead_id") or (context or {}).get("lead_id")
    appointment_id = arguments.get("appointment_id")
    if arguments.get("lead_name") and not lead_id:
        lead, err, choices = actions.resolve_lead(user_id, arguments, context)
        if err:
            return {"error": err, "choices": choices}
        lead_id = lead["id"]
    appt = scheduling.get_existing_appointment(
        user_id, lead_id=lead_id, appointment_id=appointment_id
    )
    if not appt:
        return {"appointment": None, "message": "No upcoming appointment found."}
    return {"appointment": appt}


READ_HANDLERS = {
    "find_lead": find_lead,
    "get_lead_context": get_lead_context,
    "list_lead_tasks": list_lead_tasks,
    "list_open_leads": list_open_leads,
    "get_calendar_availability": get_calendar_availability,
    "find_available_slots": find_available_slots,
    "get_existing_appointment": get_existing_appointment,
}


def normalize_write_arguments(name: str, arguments: dict) -> dict:
    return alias_location_fields(arguments)


def queue_write_tool(name: str, arguments: dict, transcript: str) -> dict:
    if not registry.is_write_tool(name):
        return {"queued": False, "error": "That action is not allowed."}
    args = normalize_write_arguments(name, arguments)
    cleaned, err = sanitize_command({"action": name, "arguments": args}, transcript)
    if cleaned is None:
        return {"queued": False, "error": err or "That action is not allowed."}
    preview = [{"label": label, "value": value} for label, value in preview_rows(cleaned)]
    if err:
        return {
            "queued": False,
            "error": err,
            "partial": cleaned,
            "preview": preview,
        }
    return {"queued": True, "action": name, "arguments": cleaned["arguments"], "preview": preview}


def dispatch_read(name: str, arguments: dict, user_id, context: dict | None) -> dict:
    handler = READ_HANDLERS.get(name)
    if not handler:
        return {"error": "Unknown tool."}
    return handler(user_id, arguments or {}, context)
