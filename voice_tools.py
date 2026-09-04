"""Vapi custom tools for CRM actions during TopAI voice conversations."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config
import crm_db
import db
import external_leads_db
from crm_constants import (
    APPOINTMENT_STATUSES,
    APPOINTMENT_TYPES,
    LEAD_STATUS_SET,
    LEGACY_STATUS_MAP,
    PRIORITIES,
    SMS_CONSENT_STATUS_LABELS,
    TASK_TYPES,
    normalize_lead_status,
    sms_consent_label,
    status_label,
)


OPEN_LEAD_EXCLUDED_STATUSES = {"closed_won", "closed_lost", "do_not_contact"}
SMS_CONSENT_ALIASES = {
    "sms verified": "verified",
    "sms_verified": "verified",
    "verified": "verified",
    "certified": "user_certified",
    "sms certified": "user_certified",
    "user certified": "user_certified",
    "user_certified": "user_certified",
    "unverified": "unverified",
    "not certified": "not_certified",
    "not_certified": "not_certified",
    "not permitted": "not_permitted",
    "not_permitted": "not_permitted",
    "opted out": "opted_out",
    "opted_out": "opted_out",
    "revoked": "revoked",
}


LIVE_VOICE_TOKEN_SALT = "topai-live-voice-account-v1"
LIVE_VOICE_TOKEN_MAX_AGE_SECONDS = 12 * 60 * 60


def create_live_voice_account_token(user_id):
    serializer = URLSafeTimedSerializer(config.FLASK_SECRET_KEY, salt=LIVE_VOICE_TOKEN_SALT)
    return serializer.dumps({"user_id": int(user_id)})


def resolve_live_voice_account_token(token):
    if not token:
        return None
    serializer = URLSafeTimedSerializer(config.FLASK_SECRET_KEY, salt=LIVE_VOICE_TOKEN_SALT)
    try:
        payload = serializer.loads(token, max_age=LIVE_VOICE_TOKEN_MAX_AGE_SECONDS)
        return int(payload.get("user_id"))
    except (BadSignature, SignatureExpired, TypeError, ValueError, AttributeError):
        return None


def voice_tool_definitions(server_url, account_token=None, template_account_token=True):
    """Return Vapi function tools the assistant can use for CRM work."""
    server = {"url": server_url}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_open_leads",
                "description": (
                    "Count or list every active/open CRM lead for this account. "
                    "Use this when the agent asks how many leads are currently open, "
                    "or asks to hear open leads by name with current status, SMS "
                    "consent, next action, and latest update."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of leads to return.",
                        }
                    },
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "update_lead_status",
                "description": (
                    "Update a CRM lead's pipeline status. Use only after the user "
                    "names the lead or provides its id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "status": {
                            "type": "string",
                            "description": "New pipeline status, such as qualified or closed_won.",
                        },
                    },
                    "required": ["status"],
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "update_lead_sms_consent_status",
                "description": (
                    "Update a lead's SMS consent state, including marking the lead "
                    "SMS Verified when consent is established."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "sms_consent_status": {
                            "type": "string",
                            "description": "One of verified, user_certified, unverified, not_certified, not_permitted, opted_out, or revoked.",
                        },
                    },
                    "required": ["sms_consent_status"],
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "schedule_lead_follow_up",
                "description": (
                    "Create or reschedule a lead follow-up on the CRM calendar. "
                    "Use this when the agent asks to set, move, schedule, or "
                    "reschedule a follow-up date/time for a lead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "due_at": {
                            "type": "string",
                            "description": "ISO-8601 timestamp for the follow-up, including timezone when known.",
                        },
                        "reason": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "description": "One of low, normal, high, or urgent.",
                        },
                        "local_due_label": {
                            "type": "string",
                            "description": "Human label the user said, such as Saturday at noon.",
                        },
                    },
                    "required": ["due_at"],
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "complete_lead_follow_up",
                "description": (
                    "Mark an open follow-up complete for a lead. Use when the "
                    "agent says a follow-up was handled, completed, done, or no "
                    "longer needs to remain open because contact happened."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "follow_up_id": {"type": "integer"},
                    },
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "create_lead_task",
                "description": (
                    "Create a CRM task tied to a lead. Use this for reminders "
                    "or non-follow-up work like prepare materials, send a note, "
                    "confirm details, or make a phone call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "due_at": {"type": "string"},
                        "priority": {"type": "string"},
                        "task_type": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "create_lead_appointment",
                "description": (
                    "Create a scheduled or confirmed appointment for a lead. "
                    "Use when the agent asks to put a meeting, showing, call, "
                    "consultation, or confirmed appointment on the calendar."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "appointment_type": {"type": "string"},
                        "start_at": {"type": "string"},
                        "end_at": {"type": "string"},
                        "location": {"type": "string"},
                        "notes": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["start_at"],
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "record_lead_update",
                "description": (
                    "Record a CRM note, contact attempt, voicemail, confirmation, "
                    "or next-action update for a lead. Use this for updates that "
                    "are factual notes rather than pipeline-stage changes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "note": {"type": "string"},
                        "next_action": {"type": "string"},
                        "status": {"type": "string"},
                        "contacted": {"type": "boolean"},
                    },
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "draft_lead_email",
                "description": (
                    "Save an email draft on a CRM lead's timeline. The user can review "
                    "and send it later."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["subject", "body"],
                },
            },
            "server": server,
        },
    ]
    if account_token:
        value = "{{topai_account_token}}" if template_account_token else account_token
        for tool in tools:
            tool["parameters"] = [
                {"key": "topai_account_token", "value": value}
            ]
    return tools


def resolve_voice_tool_user_id(payload):
    """Resolve tenant from trusted Vapi call metadata or an existing call row."""
    message = payload.get("message") if isinstance(payload, dict) else {}
    message = message if isinstance(message, dict) else {}
    call = message.get("call") or payload.get("call") or {}
    metadata = message.get("metadata") or call.get("metadata") or payload.get("metadata") or {}

    # In browser calls, Vapi injects this signed, LLM-invisible value into every
    # tool call from assistantOverrides.variableValues via static parameters.
    for tool_call in _tool_calls(payload):
        _name, args = _tool_name_and_args(tool_call)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        token_user_id = resolve_live_voice_account_token(
            args.get("topai_account_token") if isinstance(args, dict) else None
        )
        if token_user_id:
            return token_user_id

    internal_call_id = metadata.get("topai_call_id") or metadata.get("call_id")
    if internal_call_id:
        try:
            call_id = int(internal_call_id)
        except (TypeError, ValueError):
            call_id = None
        if call_id is not None:
            with db.get_db() as conn:
                row = conn.execute("SELECT user_id FROM voice_calls WHERE id = ?", (call_id,)).fetchone()
                if row:
                    return row["user_id"]

    provider_call_id = call.get("id") or message.get("callId") or payload.get("id")
    call_row = db.get_voice_call_by_provider_id(provider_call_id)
    return call_row.get("user_id") if call_row else None


def handle_vapi_tool_calls(payload):
    """Dispatch Vapi tool-calls messages and return Vapi's expected results shape."""
    user_id = resolve_voice_tool_user_id(payload)
    if not user_id:
        return {"results": _tool_results(payload, "I could not identify the TopAI account for this call.")}
    return {"results": [_handle_tool_call(user_id, call) for call in _tool_calls(payload)]}


def is_vapi_tool_call_payload(payload):
    message = payload.get("message") if isinstance(payload, dict) else {}
    message = message if isinstance(message, dict) else {}
    return (message.get("type") or payload.get("type")) == "tool-calls"


def _tool_calls(payload):
    message = payload.get("message") if isinstance(payload, dict) else {}
    message = message if isinstance(message, dict) else {}
    calls = message.get("toolCallList") or payload.get("toolCallList") or []
    if calls:
        return calls
    wrapped = message.get("toolWithToolCallList") or []
    return [item.get("toolCall") for item in wrapped if item.get("toolCall")]


def _tool_results(payload, result):
    calls = _tool_calls(payload)
    if not calls:
        return [{"toolCallId": "unknown", "result": result}]
    return [{"toolCallId": _tool_call_id(call), "result": result} for call in calls]


def _tool_call_id(call):
    return str((call or {}).get("id") or (call or {}).get("toolCallId") or "unknown")


def _tool_name_and_args(call):
    call = call or {}
    if call.get("function"):
        fn = call.get("function") or {}
        return fn.get("name") or call.get("name"), fn.get("arguments") or fn.get("parameters") or {}
    return call.get("name"), call.get("arguments") or call.get("parameters") or {}


def _handle_tool_call(user_id, call):
    tool_id = _tool_call_id(call)
    name, args = _tool_name_and_args(call)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    args = args if isinstance(args, dict) else {}
    handlers = {
        "list_open_leads": _list_open_leads,
        "update_lead_status": _update_lead_status,
        "update_lead_sms_consent_status": _update_lead_sms_consent_status,
        "schedule_lead_follow_up": _schedule_lead_follow_up,
        "complete_lead_follow_up": _complete_lead_follow_up,
        "create_lead_task": _create_lead_task,
        "create_lead_appointment": _create_lead_appointment,
        "record_lead_update": _record_lead_update,
        "draft_lead_email": _draft_lead_email,
    }
    handler = handlers.get(name)
    if not handler:
        return {"toolCallId": tool_id, "result": f"Unknown TopAI voice tool: {name}."}
    try:
        result = handler(user_id, args)
    except Exception:
        result = "TopAI could not complete that CRM action."
    return {"toolCallId": tool_id, "result": result}


def _lead_summary(lead):
    return {
        "id": lead["id"],
        "name": lead.get("name") or "Unnamed lead",
        "phone_number": lead.get("phone_number"),
        "email": lead.get("email"),
        "status": normalize_lead_status(lead.get("status")),
        "status_label": status_label(lead.get("status")),
        "sms_consent_status": lead.get("sms_consent_status") or "unverified",
        "sms_consent_label": sms_consent_label(lead.get("sms_consent_status")),
        "next_action": lead.get("next_action"),
        "next_follow_up_at": lead.get("next_follow_up_at") or lead.get("follow_up_at"),
        "property_interest": lead.get("property_interest"),
        "updated_at": lead.get("updated_at"),
    }


def _list_open_leads(user_id, args):
    limit = args.get("limit") or 50
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    total_count = crm_db.count_filtered_leads(user_id, scope="active")
    leads = [
        _lead_summary(lead)
        for lead in crm_db.filter_leads(user_id, scope="active", limit=limit)
        if normalize_lead_status(lead.get("status")) not in OPEN_LEAD_EXCLUDED_STATUSES
    ]
    if total_count == 0:
        return {"count": 0, "summary": "There are no open leads right now.", "leads": []}
    names = ", ".join(lead["name"] for lead in leads)
    lead_word = "lead" if total_count == 1 else "leads"
    return {
        "count": total_count,
        "summary": (
            f"There are {total_count} open {lead_word} right now. "
            f"Open leads returned: {names}. Walk through each returned lead by name "
            "and current status only if the agent asks for details."
        ),
        "leads": leads,
    }


def _find_lead(user_id, args):
    lead_id = args.get("lead_id")
    if lead_id not in (None, ""):
        try:
            lead = db.get_lead(int(lead_id), user_id)
        except (TypeError, ValueError):
            lead = None
        if lead:
            return lead, None
    name = str(args.get("lead_name") or "").strip().lower()
    if not name:
        return None, "I need the lead name or id."
    matches = [
        lead for lead in db.list_leads(user_id, limit=500)
        if name in str(lead.get("name") or "").strip().lower()
    ]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"I could not find a lead named {args.get('lead_name')}."
    return None, "I found more than one matching lead. Please use the lead id or full name."


def _update_lead_status(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    raw_status = str(args.get("status") or "").strip().lower().replace(" ", "_")
    candidate = LEGACY_STATUS_MAP.get(raw_status, raw_status)
    if candidate not in LEAD_STATUS_SET:
        return "That is not a supported lead status."
    new_status = normalize_lead_status(candidate)
    lead, error = crm_db.set_lead_status(user_id, lead["id"], new_status, actor_user_id=user_id)
    if error:
        return error
    return {
        "ok": True,
        "lead": _lead_summary(lead),
        "summary": f"{lead.get('name') or 'Lead'} is now {status_label(lead.get('status'))}.",
    }


def _normalize_sms_consent_status(value):
    key = str(value or "").strip().lower().replace("-", "_")
    key = key.replace("_", " ")
    return SMS_CONSENT_ALIASES.get(key) or SMS_CONSENT_ALIASES.get(key.replace(" ", "_"))


def _update_lead_sms_consent_status(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    new_status = _normalize_sms_consent_status(args.get("sms_consent_status"))
    if not new_status or new_status not in SMS_CONSENT_STATUS_LABELS:
        return "That is not a supported SMS consent status."
    sms_sending_blocked = new_status not in {"verified", "user_certified"}
    ok = external_leads_db.set_lead_sms_consent_state(
        lead["id"],
        user_id,
        sms_consent_status=new_status,
        sms_sending_blocked=sms_sending_blocked,
        actor_user_id=user_id,
        source="voice_tool",
        metadata={"requested_at": datetime.now(timezone.utc).isoformat()},
    )
    if not ok:
        return "I could not update that lead's SMS consent status."
    updated = db.get_lead(lead["id"], user_id)
    crm_db.add_lead_activity(
        lead["id"],
        user_id,
        "sms_consent_status_change",
        f"SMS consent changed to {sms_consent_label(new_status)} by voice assistant",
        {"sms_consent_status": new_status, "sms_sending_blocked": sms_sending_blocked},
        actor_user_id=user_id,
    )
    return {
        "ok": True,
        "lead": _lead_summary(updated),
        "summary": f"{updated.get('name') or 'Lead'} is now SMS {sms_consent_label(new_status)}.",
    }


def _normalize_priority(value):
    priority = str(value or "normal").strip().lower().replace(" ", "_")
    return priority if priority in PRIORITIES else "normal"


def _schedule_lead_follow_up(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    due_at = str(args.get("due_at") or "").strip()
    if not due_at:
        return "I need a due date and time for the follow-up."
    result, error = crm_db.set_lead_follow_up(
        user_id,
        lead["id"],
        due_at,
        str(args.get("reason") or "Follow up").strip()[:500],
        priority=_normalize_priority(args.get("priority")),
        created_by=user_id,
        replace_existing=True,
        local_due_label=str(args.get("local_due_label") or "").strip(),
    )
    if error:
        return error
    updated = db.get_lead(lead["id"], user_id)
    return {
        "ok": True,
        "follow_up": result,
        "lead": _lead_summary(updated),
        "summary": result.get("confirmation") or f"Follow-up scheduled for {lead.get('name') or 'the lead'}.",
    }


def _complete_lead_follow_up(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    follow_up_id = args.get("follow_up_id")
    try:
        follow_up_id = int(follow_up_id) if follow_up_id not in (None, "") else None
    except (TypeError, ValueError):
        return "That follow-up id is not valid."
    ok, error = crm_db.complete_lead_follow_up(user_id, lead["id"], follow_up_id=follow_up_id)
    if error:
        return error
    updated = db.get_lead(lead["id"], user_id)
    return {
        "ok": bool(ok),
        "lead": _lead_summary(updated),
        "summary": f"Completed the open follow-up for {updated.get('name') or 'the lead'}.",
    }


def _create_lead_task(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    title = str(args.get("title") or "").strip()[:200]
    if not title:
        return "I need a task title."
    task_type = str(args.get("task_type") or "general_follow_up").strip().lower()
    task_id, error = crm_db.create_task(user_id, {
        "lead_id": lead["id"],
        "title": title,
        "description": str(args.get("description") or "").strip()[:2000],
        "due_at": str(args.get("due_at") or "").strip() or None,
        "priority": _normalize_priority(args.get("priority")),
        "task_type": task_type if task_type in TASK_TYPES else "general_follow_up",
    })
    if error:
        return error
    return {
        "ok": True,
        "task_id": task_id,
        "lead": _lead_summary(lead),
        "summary": f"Created task for {lead.get('name') or 'the lead'}: {title}.",
    }


def _create_lead_appointment(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    start_at = str(args.get("start_at") or "").strip()
    if not start_at:
        return "I need an appointment start date and time."
    appointment_type = str(args.get("appointment_type") or "phone_call").strip().lower()
    appointment_status = str(args.get("status") or "scheduled").strip().lower()
    appointment_id, error = crm_db.create_appointment(user_id, {
        "lead_id": lead["id"],
        "appointment_type": appointment_type if appointment_type in APPOINTMENT_TYPES else "phone_call",
        "start_at": start_at,
        "end_at": str(args.get("end_at") or "").strip() or None,
        "location": str(args.get("location") or "").strip()[:500],
        "notes": str(args.get("notes") or "").strip()[:2000],
        "status": appointment_status if appointment_status in APPOINTMENT_STATUSES else "scheduled",
    })
    if error:
        return error
    updated = db.get_lead(lead["id"], user_id)
    return {
        "ok": True,
        "appointment_id": appointment_id,
        "lead": _lead_summary(updated),
        "summary": f"Created appointment for {updated.get('name') or 'the lead'} at {start_at}.",
    }


def _record_lead_update(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    note = str(args.get("note") or "").strip()[:1500]
    next_action = str(args.get("next_action") or "").strip()[:500] or None
    raw_status = str(args.get("status") or "").strip().lower().replace(" ", "_")
    candidate = LEGACY_STATUS_MAP.get(raw_status, raw_status) if raw_status else None
    if candidate and candidate not in LEAD_STATUS_SET:
        return "That is not a supported lead status."
    if args.get("contacted"):
        db.touch_lead_outbound(lead["id"], user_id)
    if note or next_action:
        db.merge_lead_call_outcome_notes(
            lead["id"],
            user_id,
            summary=note or None,
            next_action=next_action,
        )
    if candidate:
        updated, error = crm_db.set_lead_status(
            user_id,
            lead["id"],
            normalize_lead_status(candidate),
            actor_user_id=user_id,
        )
        if error:
            return error
    else:
        updated = db.get_lead(lead["id"], user_id)
    if note and not next_action:
        crm_db.add_lead_activity(
            lead["id"],
            user_id,
            "voice_note",
            f"Voice assistant note: {note[:240]}",
            {"note": note},
            actor_user_id=user_id,
        )
    return {
        "ok": True,
        "lead": _lead_summary(updated),
        "summary": f"Updated {updated.get('name') or 'the lead'}.",
    }


def _draft_lead_email(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    subject = str(args.get("subject") or "").strip()[:200]
    body = str(args.get("body") or "").strip()[:3000]
    if not subject or not body:
        return "I need both a subject and body to draft an email."
    activity_id = crm_db.add_lead_activity(
        lead["id"],
        user_id,
        "email_draft_created",
        f"Email draft created: {subject}",
        {"subject": subject, "body": body, "lead_email": lead.get("email")},
        actor_user_id=user_id,
    )
    return {
        "ok": True,
        "activity_id": activity_id,
        "lead": _lead_summary(lead),
        "draft": {"subject": subject, "body": body},
        "summary": f"Email draft saved for {lead.get('name') or 'the lead'}: {subject}.",
    }
