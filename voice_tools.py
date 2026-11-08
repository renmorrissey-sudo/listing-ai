"""Vapi custom tools for CRM actions during TopAI voice conversations."""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

# Spoken-friendly refusals returned as the tool result when the harness
# policy gate does not allow a Vapi tool to execute. The handler is never
# run in these cases, so no write/send reaches a provider.
_HUMAN_APPROVAL_REFUSAL = (
    "That action isn't available automatically — it needs human approval."
)
_TRY_LATER_REFUSAL = (
    "That action can't be completed right now — please try again later."
)
# Matches Ask TopAI's wording for final lead statuses (ask_topai/actions.py
# execute_update_status / autonomy.allowed_auto_status).
_FINAL_STATUS_REFUSAL = "That status cannot be applied automatically."

# Fallback copy of autonomy.FINAL_LEAD_STATUSES, used only if autonomy
# cannot be imported (fail closed: refuse the status rather than allow it).
_FINAL_LEAD_STATUSES_FALLBACK = frozenset(
    {
        "closed_won",
        "closed_lost",
        "do_not_contact",
        "under_contract",
        "appointment_completed",
    }
)

# Vapi write tools. In drafts_only mode every one of these must become a
# draft / refusal, never an execution. This set exists because
# harness.policy._is_write() only recognises the Ask TopAI registry's
# WRITE_TOOLS, so decide() returns ALLOW for update_lead_contact_info in
# drafts_only mode; the gate below overrides that ALLOW to DRAFT for these
# tools when the tenant mode is drafts_only.
_VOICE_WRITE_TOOLS = frozenset(
    {
        "update_lead_status",
        "update_lead_sms_consent_status",
        "update_lead_contact_info",
        "send_lead_email",
    }
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
                            "description": "New pipeline status, such as qualified.",
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
                    "Compose and send a one-to-one email to a CRM lead using the "
                    "account's connected SendGrid email integration. Use the user's "
                    "plain-language request to write the subject and body."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {"type": "integer"},
                        "lead_name": {"type": "string"},
                        "subject": {
                            "type": "string",
                            "description": "The subject line you wrote for the requested email.",
                        },
                        "body": {
                            "type": "string",
                            "description": "The complete email body you wrote from the user's request.",
                        },
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


def _final_lead_statuses():
    """Final/irreversible lead statuses, never auto-applied (autonomy)."""
    try:
        import autonomy

        return frozenset(autonomy.FINAL_LEAD_STATUSES)
    except Exception as exc:  # fail closed: fall back to the known set
        logger.warning("voice_tools could not import autonomy: %s", exc)
        return _FINAL_LEAD_STATUSES_FALLBACK


def _normalized_status_arg(args):
    """Normalize an update_lead_status status argument, as the handler does."""
    raw_status = str(args.get("status") or "").strip().lower().replace(" ", "_")
    candidate = LEGACY_STATUS_MAP.get(raw_status, raw_status)
    if candidate not in LEAD_STATUS_SET:
        return ""
    return normalize_lead_status(candidate)


def _resolve_lead_id_for_audit(user_id, args):
    """Best-effort lead id for the audit row. Read-only; never writes."""
    try:
        lead, error = _find_lead(user_id, args)
        if lead and not error:
            return lead.get("id")
    except Exception as exc:
        logger.warning("voice_tools audit lead lookup failed: %s", exc)
    return None


def _record_gate_audit(user_id, name, target_id, mode, decision, reason):
    """Best-effort harness audit row; never breaks the voice call."""
    try:
        from harness.audit import record_audit

        record_audit(
            user_id,
            "ai",
            "vapi_tool_call",
            tool=name,
            target_type="lead",
            target_id=target_id,
            mode=mode,
            gate_decision=decision,
            gate_reason=reason,
        )
    except Exception as exc:
        # E.g. harness tables not migrated yet — log and continue.
        logger.warning("voice_tools gate audit failed: %s", exc)


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

    # Final lead statuses are refused in ALL modes, matching Ask TopAI
    # (autonomy.allowed_auto_status returns None for them). Checked before
    # the policy gate so no mode can re-enable them.
    if name == "update_lead_status" and _normalized_status_arg(args) in _final_lead_statuses():
        reason = "final lead status cannot be applied automatically"
        mode = None
        try:
            from harness.policy import get_settings

            mode = get_settings(user_id).get("mode")
        except Exception:
            pass
        _record_gate_audit(
            user_id, name, _resolve_lead_id_for_audit(user_id, args),
            mode, "BLOCK", reason,
        )
        return {"toolCallId": tool_id, "result": _FINAL_STATUS_REFUSAL}

    # Harness policy gate (lazy import; fail closed if unavailable).
    try:
        from harness import policy as harness_policy
    except Exception as exc:
        logger.warning("voice_tools harness policy unavailable: %s", exc)
        _record_gate_audit(
            user_id, name, _resolve_lead_id_for_audit(user_id, args),
            None, "DRAFT", "harness policy unavailable; human review required",
        )
        return {"toolCallId": tool_id, "result": _HUMAN_APPROVAL_REFUSAL}
    try:
        decision, reason = harness_policy.decide(user_id, name, name)
    except Exception as exc:
        logger.warning("voice_tools policy decide failed: %s", exc)
        decision, reason = "DRAFT", "policy gate error; human review required"
    try:
        mode = harness_policy.get_settings(user_id).get("mode")
    except Exception:
        mode = "drafts_only"

    # Compensate for a harness-policy registry gap: decide() returns ALLOW
    # for update_lead_contact_info in drafts_only mode because that tool is
    # not in ask_topai.registry.WRITE_TOOLS. In drafts_only every Vapi
    # write is a draft, never an execution, so override ALLOW to DRAFT.
    if mode == "drafts_only" and name in _VOICE_WRITE_TOOLS and decision == "ALLOW":
        decision = "DRAFT"
        reason = (
            "drafts_only mode: writes are drafts pending human approval"
        )

    target_id = _resolve_lead_id_for_audit(user_id, args)

    if decision == "ALLOW":
        _record_gate_audit(user_id, name, target_id, mode, decision, reason)
        try:
            result = handler(user_id, args)
        except Exception:
            result = "TopAI could not complete that CRM action."
        return {"toolCallId": tool_id, "result": result}

    if decision == "BLOCK":
        _record_gate_audit(user_id, name, target_id, mode, decision, reason)
        refusal = (
            _FINAL_STATUS_REFUSAL
            if name == "update_lead_status"
            else _HUMAN_APPROVAL_REFUSAL
        )
        return {"toolCallId": tool_id, "result": refusal}

    if decision == "DEFER":
        _record_gate_audit(user_id, name, target_id, mode, decision, reason)
        return {"toolCallId": tool_id, "result": _TRY_LATER_REFUSAL}

    # DRAFT (and any unexpected decision, treated as DRAFT — fail closed):
    # do not execute, except draft_lead_email, whose handler only saves a
    # draft record for human review — that IS the Drafts Only behaviour.
    _record_gate_audit(user_id, name, target_id, mode, "DRAFT", reason)
    if name == "draft_lead_email":
        try:
            result = handler(user_id, args)
        except Exception:
            result = "TopAI could not complete that CRM action."
        return {"toolCallId": tool_id, "result": result}
    return {"toolCallId": tool_id, "result": _HUMAN_APPROVAL_REFUSAL}


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
