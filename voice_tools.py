"""Vapi custom tools for CRM actions during TopAI voice conversations."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import crm_db
import db
import external_leads_db
import lead_contact_service
from lead_email_service import send_lead_email
from crm_constants import (
    LEAD_STATUS_SET,
    LEGACY_STATUS_MAP,
    SMS_CONSENT_STATUS_LABELS,
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


def voice_tool_definitions(server_url):
    """Return Vapi function tools the assistant can use for CRM work."""
    server = {"url": server_url}
    return [
        {
            "type": "function",
            "function": {
                "name": "list_open_leads",
                "description": (
                    "List every active/open CRM lead for this account by name with "
                    "current status, SMS consent, next action, and latest update."
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
        {
            "type": "function",
            "function": {
                "name": "update_lead_contact_info",
                "description": (
                    "Update a CRM lead's contact details, including phone number, "
                    "email address, name, lead type, property interest, notes, or next action."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "name": {"type": "string"},
                        "phone_number": {"type": "string"},
                        "email": {"type": "string"},
                        "lead_type": {"type": "string"},
                        "property_interest": {"type": "string"},
                        "notes": {"type": "string"},
                        "next_action": {"type": "string"},
                    },
                },
            },
            "server": server,
        },
        {
            "type": "function",
            "function": {
                "name": "send_lead_email",
                "description": (
                    "Send a one-to-one email to a CRM lead using the account's "
                    "connected SendGrid email integration."
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


def resolve_voice_tool_user_id(payload):
    """Resolve tenant from trusted Vapi call metadata or an existing call row."""
    message = payload.get("message") if isinstance(payload, dict) else {}
    message = message if isinstance(message, dict) else {}
    call = message.get("call") or payload.get("call") or {}
    metadata = message.get("metadata") or call.get("metadata") or payload.get("metadata") or {}

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
        "update_lead_contact_info": _update_lead_contact_info,
        "draft_lead_email": _draft_lead_email,
        "send_lead_email": _send_lead_email,
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
    leads = [
        _lead_summary(lead)
        for lead in crm_db.filter_leads(user_id, scope="active", limit=limit)
        if normalize_lead_status(lead.get("status")) not in OPEN_LEAD_EXCLUDED_STATUSES
    ]
    if not leads:
        return {"count": 0, "summary": "There are no open leads right now.", "leads": []}
    names = ", ".join(lead["name"] for lead in leads)
    return {
        "count": len(leads),
        "summary": f"Open leads: {names}. Walk through each lead by name and current status.",
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


def _update_lead_contact_info(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    contact_args = {
        key: args[key]
        for key in (
            "name",
            "phone_number",
            "phone",
            "email",
            "lead_type",
            "property_interest",
            "notes",
            "next_action",
        )
        if key in args
    }
    updated, error, _status_code = lead_contact_service.update_lead_contact_info(
        user_id,
        lead["id"],
        contact_args,
        actor_user_id=user_id,
        source="voice_tool",
    )
    if error:
        return error
    return {
        "ok": True,
        "lead": _lead_summary(updated),
        "updated_fields": [
            ("phone_number" if key == "phone" else key)
            for key in contact_args
            if (lead.get("phone_number" if key == "phone" else key) or "")
            != (updated.get("phone_number" if key == "phone" else key) or "")
        ],
        "summary": f"Contact info updated for {updated.get('name') or 'the lead'}.",
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


def _send_lead_email(user_id, args):
    lead, error = _find_lead(user_id, args)
    if error:
        return error
    subject = str(args.get("subject") or "").strip()[:200]
    body = str(args.get("body") or "").strip()[:5000]
    result, error = send_lead_email(
        user_id,
        lead["id"],
        subject=subject,
        body=body,
        actor_user_id=user_id,
    )
    if error:
        return error
    return {
        "ok": True,
        "lead": _lead_summary(lead),
        "email": result,
        "summary": (
            f"Email sent to {result.get('to_email')} for "
            f"{lead.get('name') or 'the lead'}: {subject}."
        ),
    }
