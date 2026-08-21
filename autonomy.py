"""Single TopAI autonomy policy.

Routine CRM, SMS, and scheduling actions execute when identity, data,
permission, and compliance are clear. Human review is the exception.
"""

from __future__ import annotations

import json

import crm_db
import db
from ask_topai import registry
from crm_constants import normalize_lead_status

MODE_AUTO = "auto_execute"
MODE_CLARIFY = "clarify"
MODE_BLOCK = "block"

# Destructive / financial / consent tools never auto-execute.
BLOCK_TOOLS = frozenset(
    {
        "delete_lead",
        "delete_records",
        "bulk_action",
        "change_consent",
        "change_sms_qualification",
        "sql",
        "execute_sql",
        "raw_query",
        "send_email",
        "draft_email",
        "send_listings",
        "initiate_ai_call",
        "place_call",
        "start_call",
        "send_sms",
        "draft_sms",
    }
)

# SMS auto-reply still stops for these coach escalation topics.
SMS_ESCALATION_BLOCK = frozenset(
    {
        "legal",
        "financing",
        "fair_housing",
        "complaint",
        "negotiation",
        "uncertain_property_fact",
    }
)

# Statuses TopAI may apply from clear evidence. Final/irreversible states stay
# agent-owned unless a dedicated compliance path (opt-out) already ran.
ROUTINE_LEAD_STATUSES = frozenset(
    {
        "new",
        "attempting_contact",
        "contacted",
        "qualified",
        "appointment_scheduled",
        "nurture",
    }
)

FINAL_LEAD_STATUSES = frozenset(
    {
        "closed_won",
        "closed_lost",
        "do_not_contact",
        "under_contract",
        "appointment_completed",
    }
)


def tool_mode(tool_name: str) -> str:
    name = (tool_name or "").strip()
    if not name:
        return MODE_BLOCK
    if name in BLOCK_TOOLS:
        return MODE_BLOCK
    if registry.is_read_tool(name) or name in registry.WRITE_TOOLS or name in registry.CONTROL_TOOLS:
        return MODE_AUTO
    if registry.is_future_tool(name):
        return MODE_BLOCK
    return MODE_BLOCK


def should_auto_execute_tool(tool_name: str) -> bool:
    return tool_mode(tool_name) == MODE_AUTO


def sms_escalation_topics(analysis: dict | None) -> set[str]:
    data = analysis or {}
    topics = set()
    for topic in data.get("escalation_topics") or []:
        cleaned = str(topic or "").strip().lower().replace(" ", "_").replace("-", "_")
        if cleaned in SMS_ESCALATION_BLOCK:
            topics.add(cleaned)
    return topics


def should_auto_send_sms(analysis: dict | None, lead: dict | None = None) -> bool:
    """Routine inbound replies send automatically. Compliance/escalation override."""
    data = analysis or {}
    lead = lead or {}
    if (lead.get("opt_out_status") or "active") == "opted_out":
        return False
    consent = (lead.get("sms_consent_status") or "").lower()
    if consent in {"opted_out", "revoked", "not_permitted", "suppressed", "invalid_number"}:
        return False
    if data.get("sensitive_topic") or sms_escalation_topics(data):
        return False
    draft = str(data.get("draft_reply") or data.get("suggested_reply") or "").strip()
    return bool(draft)


def allowed_auto_status(status: str) -> str | None:
    normalized = normalize_lead_status(status)
    if not normalized or normalized in FINAL_LEAD_STATUSES:
        return None
    if normalized in ROUTINE_LEAD_STATUSES:
        return normalized
    return None


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


def _append_note(user_id, lead_id, note: str, *, source="ai_sms"):
    from ask_topai.actions import append_note
    from ask_topai.audit import add_lead_audit

    text = (note or "").strip()
    if not text:
        return None
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None
    existing = (lead.get("notes") or "").strip()
    if text.lower() in existing.lower():
        return lead
    updated, err = append_note(user_id, lead_id, text)
    if err or not updated:
        return None
    add_lead_audit(
        user_id,
        lead_id,
        f"AI captured a note for {lead.get('name') or 'lead'}",
        {"action": "record_conversation_context", "source": source},
    )
    return updated


def _find_open_task(user_id, lead_id, title: str):
    needle = (title or "").strip().lower()
    if not needle:
        return None
    for task in crm_db.list_tasks(user_id, limit=200):
        if task.get("status") not in {"open", "in_progress"}:
            continue
        if lead_id and task.get("lead_id") != lead_id:
            continue
        if (task.get("title") or "").strip().lower() == needle:
            return task
    return None


def apply_inbound_side_effects(user_id, lead_id, analysis: dict | None, *, source="ai_sms"):
    """Apply routine CRM captures from an inbound SMS analysis. Never closes/lost."""
    analysis = analysis or {}
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return {"applied": []}
    applied = []

    note = str(analysis.get("captured_note") or "").strip()
    if note:
        if _append_note(user_id, lead_id, note, source=source):
            applied.append("note")

    incoming = analysis.get("property_criteria_updates")
    if isinstance(incoming, dict) and incoming:
        from ask_topai.actions import merge_property_criteria, _save_criteria
        from ask_topai.audit import add_lead_audit

        merged = merge_property_criteria(_criteria_dict(lead), incoming, replace=False)
        if merged != _criteria_dict(lead):
            _save_criteria(user_id, lead_id, merged)
            add_lead_audit(
                user_id,
                lead_id,
                f"AI updated property criteria for {lead.get('name') or 'lead'}",
                {"action": "update_property_criteria", "source": source},
            )
            applied.append("criteria")
            lead = db.get_lead(lead_id, user_id)

    intent = str(analysis.get("intent") or "").lower()
    if any(k in intent for k in ("not interested", "no longer interested", "stop pursuing")):
        crm_db.set_lead_status(user_id, lead_id, "nurture", from_automation=True)
        applied.append("status")
    else:
        suggested = allowed_auto_status(analysis.get("suggested_lead_status") or "")
        current = normalize_lead_status(lead.get("status"))
        if suggested and suggested != current and current not in FINAL_LEAD_STATUSES:
            crm_db.set_lead_status(user_id, lead_id, suggested, from_automation=True)
            applied.append("status")
        elif current in {"new", "attempting_contact"} and (lead.get("opt_out_status") or "") != "opted_out":
            crm_db.set_lead_status(user_id, lead_id, "contacted", from_automation=True)
            applied.append("status")

    for item in analysis.get("suggested_tasks") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        if _find_open_task(user_id, lead_id, title):
            continue
        task_id, err = crm_db.create_task(
            user_id,
            {
                "title": title,
                "description": str(item.get("description") or "")[:2000],
                "due_at": item.get("due_at"),
                "lead_id": lead_id,
                "task_type": item.get("task_type") or "general_follow_up",
            },
        )
        if not err and task_id:
            applied.append("task")

    return {"applied": applied}
