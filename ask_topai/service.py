"""Interpret + confirm orchestration. Mutations run only after Confirm."""

from __future__ import annotations

import json

import db
from ask_topai import actions, audit
from ask_topai.parser import interpret_request
from ask_topai.schemas import preview_rows
from external_leads.duplicates import find_duplicate
from lead_service import duplicate_phone_message


def sanitize_context(user_id, raw: dict | None) -> dict:
    raw = raw or {}
    page = str(raw.get("page") or "")[:200]
    lead_id = raw.get("lead_id")
    lead = None
    try:
        lead_id = int(lead_id) if lead_id not in (None, "") else None
    except (TypeError, ValueError):
        lead_id = None
    if lead_id:
        lead = db.get_lead(lead_id, user_id)
        if not lead:
            lead_id = None
    return {
        "page": page,
        "lead_id": lead["id"] if lead else None,
        "lead_name": (lead.get("name") if lead else None),
    }


def _attach_resolved_leads(user_id, commands: list, context: dict):
    resolved = []
    for command in commands:
        item = {"action": command.get("action"), "arguments": dict(command.get("arguments") or {})}
        action = item["action"]
        if action == "create_lead" and item["arguments"].get("phone"):
            existing, _match = find_duplicate(user_id, phone=item["arguments"]["phone"])
            if existing:
                return None, duplicate_phone_message(existing), [
                    {
                        "id": existing["id"],
                        "name": existing.get("name") or "Lead",
                        "phone_number": existing.get("phone_number"),
                    }
                ]
        if action in {"add_lead_note", "update_property_criteria"} or (
            action == "create_task" and (item["arguments"].get("lead_name") or item["arguments"].get("lead_id") or context.get("lead_id"))
        ):
            lead, err, choices = actions.resolve_lead(user_id, item["arguments"], context)
            if err:
                return None, err, choices
            item["arguments"]["lead_id"] = lead["id"]
            item["arguments"]["lead_name"] = lead.get("name")
        resolved.append(item)
    return resolved, None, []


def build_preview(commands: list) -> dict:
    return {
        "title": "I understood:",
        "commands": [
            {
                "action": command["action"],
                "rows": [{"label": label, "value": value} for label, value in preview_rows(command)],
            }
            for command in commands
        ],
    }


def interpret(user_id, transcript: str, raw_context: dict | None, *, model_payload: dict | None = None):
    context = sanitize_context(user_id, raw_context)
    parsed = interpret_request(transcript, context, model_payload=model_payload)
    status = parsed["status"]
    commands = parsed.get("commands") or []
    lead_id = context.get("lead_id")

    if status != "ok":
        audit.record_command(
            user_id,
            transcript=transcript,
            interpreted=parsed,
            status=status,
            lead_id=lead_id,
            result={"message": parsed.get("message")},
        )
        return {
            "ok": True,
            "status": status,
            "message": parsed.get("message"),
            "preview": None,
            "confirmation_token": None,
            "choices": [],
        }

    resolved, err, choices = _attach_resolved_leads(user_id, commands, context)
    if err:
        audit.record_command(
            user_id,
            transcript=transcript,
            interpreted={"status": "needs_clarification", "commands": commands},
            status="needs_clarification",
            lead_id=lead_id,
            result={"message": err, "choices": choices},
        )
        return {
            "ok": True,
            "status": "needs_clarification",
            "message": err,
            "preview": None,
            "confirmation_token": None,
            "choices": choices,
        }

    token, expires = audit.issue_confirmation_token()
    target_lead = None
    for command in resolved:
        if command.get("arguments", {}).get("lead_id"):
            target_lead = command["arguments"]["lead_id"]
            break
    audit.record_command(
        user_id,
        transcript=transcript,
        interpreted={"status": "ok", "commands": resolved},
        status="pending_confirmation",
        lead_id=target_lead,
        confirmation_token=token,
        expires_at=expires,
    )
    return {
        "ok": True,
        "status": "needs_confirmation",
        "message": parsed.get("message") or "Confirm this action before I change any CRM data.",
        "preview": build_preview(resolved),
        "confirmation_token": token,
        "choices": [],
    }


def confirm(user_id, token: str, raw_context: dict | None = None):
    pending = audit.get_pending_by_token(user_id, token)
    if not pending:
        return {"ok": False, "error": "This confirmation is missing or has expired."}, 400
    context = sanitize_context(user_id, raw_context)
    try:
        interpreted = json.loads(pending.get("interpreted_json") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "Stored command is invalid."}, 400
    commands = interpreted.get("commands") or []
    if not commands:
        audit.mark_status(pending["id"], user_id, "failed", result={"error": "No commands"})
        return {"ok": False, "error": "Nothing to execute."}, 400

    results = []
    for command in commands:
        action, payload, error, choices = actions.execute_command(user_id, command, context)
        if error:
            audit.mark_status(
                pending["id"],
                user_id,
                "failed",
                result={"error": error, "completed": results, "choices": choices},
                executed=True,
            )
            body = {"ok": False, "error": error, "choices": choices or [], "completed": results}
            return body, 409 if choices else 400
        results.append(
            {
                "action": action,
                "message": actions.success_message(action, payload),
                "lead_id": (payload or {}).get("id") if action != "create_task" else command.get("arguments", {}).get("lead_id"),
                "task_id": (payload or {}).get("id") if action == "create_task" else None,
            }
        )

    audit.mark_status(pending["id"], user_id, "executed", result={"results": results}, executed=True)
    return {
        "ok": True,
        "status": "executed",
        "results": results,
        "message": " ".join(item["message"] for item in results),
    }, 200
