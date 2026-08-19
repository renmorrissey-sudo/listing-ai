"""Interpret + confirm orchestration. Mutations run only after Confirm."""

from __future__ import annotations

import json

import db
from ask_topai import actions, agent, audit
from ask_topai.parser import validate_model_payload
from ask_topai.schemas import preview_rows, sanitize_command
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


def _same_person(named: str, pending_name: str) -> bool:
    if not pending_name:
        return False
    if not named:
        return True
    named_tokens = set((named or "").lower().split())
    pending_tokens = set((pending_name or "").lower().split())
    return bool(named_tokens & pending_tokens)


def _attach_resolved_leads(user_id, commands: list, context: dict):
    resolved = []
    pending_create_name = None
    for command in commands:
        item = {"action": command.get("action"), "arguments": dict(command.get("arguments") or {})}
        action = item["action"]
        args = item["arguments"]
        if action == "create_lead":
            if args.get("phone"):
                existing, _match = find_duplicate(user_id, phone=args["phone"])
                if existing:
                    return None, duplicate_phone_message(existing), [
                        {
                            "id": existing["id"],
                            "name": existing.get("name") or "Lead",
                            "phone_number": existing.get("phone_number"),
                        }
                    ]
            pending_create_name = (args.get("name") or "").strip()
            resolved.append(item)
            continue

        needs_lead = action in {"add_lead_note", "update_property_criteria"} or (
            action == "create_task"
            and (args.get("lead_name") or args.get("lead_id") or context.get("lead_id") or pending_create_name)
        )
        if needs_lead:
            named = (args.get("lead_name") or args.get("name") or "").strip()
            if pending_create_name and not args.get("lead_id") and _same_person(named, pending_create_name):
                args["lead_name"] = named or pending_create_name
                resolved.append(item)
                continue
            lead, err, choices = actions.resolve_lead(user_id, args, context)
            if err:
                return None, err, choices
            args["lead_id"] = lead["id"]
            args["lead_name"] = lead.get("name")
        resolved.append(item)
    return resolved, None, []


def build_preview(commands: list) -> dict:
    numbered = []
    for index, command in enumerate(commands, 1):
        numbered.append(
            {
                "action": command["action"],
                "index": index,
                "rows": [{"label": label, "value": value} for label, value in preview_rows(command)],
            }
        )
    return {"title": "ASK TOPAI UNDERSTOOD:", "commands": numbered}


def _api_status(internal: str, *, has_token=False) -> str:
    if has_token or internal in {"ok", "needs_confirmation", "action_plan"}:
        return "action_plan"
    return {
        "needs_clarification": "clarification_required",
        "unsupported": "unsupported_action",
        "informational": "informational_response",
        "error": "error",
    }.get(internal, internal)


def _audit_kwargs(raw, session_id=None):
    return {
        "model": raw.get("model"),
        "input_source": raw.get("source"),
        "tools_invoked": raw.get("tools_invoked"),
        "session_key": raw.get("session_id") or session_id,
    }


def interpret(user_id, transcript: str, raw_context: dict | None, *, session_id=None, source="text"):
    context = sanitize_context(user_id, raw_context)
    source = source if source in {"voice", "text"} else "text"
    raw = agent.complete(
        user_id,
        transcript,
        context,
        session_id=session_id,
        source=source,
    )
    session_id = raw.get("session_id") or session_id
    lead_id = context.get("lead_id")
    choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []

    if raw.get("status") == "error":
        audit.record_command(
            user_id,
            transcript=transcript,
            interpreted={"status": "error", "message": raw.get("message")},
            status="error",
            lead_id=lead_id,
            result={"message": raw.get("message")},
            **_audit_kwargs(raw, session_id),
        )
        return {
            "ok": True,
            "status": "error",
            "message": raw.get("message") or "Ask TopAI could not process that.",
            "preview": None,
            "confirmation_token": None,
            "choices": [],
            "session_id": session_id,
        }

    grounding = raw.get("grounding_transcript") or transcript
    parsed = validate_model_payload(
        {
            "status": raw.get("status"),
            "message": raw.get("message"),
            "commands": raw.get("commands") or [],
        },
        grounding,
    )
    status = parsed["status"]
    commands = parsed.get("commands") or []

    if status != "ok":
        audit.record_command(
            user_id,
            transcript=transcript,
            interpreted=parsed,
            status=status,
            lead_id=lead_id,
            result={"message": parsed.get("message"), "choices": choices},
            **_audit_kwargs(raw, session_id),
        )
        return {
            "ok": True,
            "status": _api_status(status),
            "message": parsed.get("message"),
            "preview": None,
            "confirmation_token": None,
            "choices": choices,
            "session_id": session_id,
        }

    resolved, err, resolve_choices = _attach_resolved_leads(user_id, commands, context)
    if err:
        audit.record_command(
            user_id,
            transcript=transcript,
            interpreted={"status": "needs_clarification", "commands": commands},
            status="needs_clarification",
            lead_id=lead_id,
            result={"message": err, "choices": resolve_choices},
            **_audit_kwargs(raw, session_id),
        )
        return {
            "ok": True,
            "status": "clarification_required",
            "message": err,
            "preview": None,
            "confirmation_token": None,
            "choices": resolve_choices,
            "session_id": session_id,
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
        interpreted={
            "status": "ok",
            "commands": resolved,
            "grounding_transcript": grounding,
        },
        status="pending_confirmation",
        lead_id=target_lead,
        confirmation_token=token,
        expires_at=expires,
        **_audit_kwargs(raw, session_id),
    )
    return {
        "ok": True,
        "status": "action_plan",
        "message": parsed.get("message") or "Confirm this action plan before I change any CRM data.",
        "preview": build_preview(resolved),
        "confirmation_token": token,
        "choices": [],
        "session_id": session_id,
    }


def _bind_created_lead(command: dict, created_id, created_name):
    if not created_id or command.get("action") == "create_lead":
        return
    args = command.setdefault("arguments", {})
    if args.get("lead_id"):
        return
    named = (args.get("lead_name") or "").strip()
    if named and created_name and not _same_person(named, created_name):
        return
    args["lead_id"] = created_id
    if created_name and not args.get("lead_name"):
        args["lead_name"] = created_name


def _friendly_summary(results, failures):
    success = " ".join(item["message"] for item in results if item.get("message"))
    if results and not failures:
        if success.lower().startswith("done"):
            return success
        return f"Done. {success}".strip()
    if results and failures:
        fail = " ".join(item.get("error") or "an action failed" for item in failures)
        return f"{success} I couldn't complete everything: {fail}".strip()
    if failures:
        return failures[0].get("error") or "I couldn't complete that."
    return "Nothing was changed."


def confirm(user_id, token: str, raw_context: dict | None = None):
    row = audit.get_by_token(user_id, token)
    if not row:
        pending = audit.get_pending_by_token(user_id, token)
        row = pending
    if not row:
        return {"ok": False, "error": "This confirmation is missing or has expired."}, 400
    if row.get("status") in {"executed", "partial"}:
        try:
            previous = json.loads(row.get("result_json") or "{}")
        except json.JSONDecodeError:
            previous = {}
        return {
            "ok": True,
            "status": row.get("status"),
            "message": previous.get("message") or "This request was already completed.",
            "results": previous.get("results") or [],
            "failures": previous.get("failures") or [],
            "duplicate": True,
        }, 200
    if row.get("status") != "pending_confirmation":
        return {"ok": False, "error": "This confirmation is missing or has expired."}, 400
    expires = row.get("expires_at") or ""
    from datetime import datetime, timezone

    if expires and expires < datetime.now(timezone.utc).isoformat():
        audit.mark_status(row["id"], user_id, "expired")
        return {"ok": False, "error": "This confirmation is missing or has expired."}, 400

    context = sanitize_context(user_id, raw_context)
    try:
        interpreted = json.loads(row.get("interpreted_json") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "Stored command is invalid."}, 400
    commands = interpreted.get("commands") or []
    grounding = interpreted.get("grounding_transcript") or row.get("transcript") or ""
    if not commands:
        audit.mark_status(row["id"], user_id, "failed", result={"error": "No commands"})
        return {"ok": False, "error": "Nothing to execute."}, 400

    results = []
    failures = []
    created_id = None
    created_name = None
    for command in commands:
        command = {"action": command.get("action"), "arguments": dict(command.get("arguments") or {})}
        _bind_created_lead(command, created_id, created_name)
        cleaned, err = sanitize_command(command, grounding)
        if cleaned is None or err:
            failures.append(
                {
                    "action": command.get("action"),
                    "error": err or "That command is not allowed.",
                    "choices": [],
                }
            )
            continue
        lead_id = cleaned.get("arguments", {}).get("lead_id")
        if lead_id and not db.get_lead(lead_id, user_id):
            failures.append(
                {
                    "action": cleaned.get("action"),
                    "error": "Lead not found.",
                    "choices": [],
                }
            )
            continue
        action, payload, error, choices = actions.execute_command(user_id, cleaned, context)
        if error:
            failures.append({"action": action, "error": error, "choices": choices or []})
            continue
        item = {
            "action": action,
            "message": actions.success_message(action, payload),
            "lead_id": (payload or {}).get("id") if action != "create_task" else cleaned.get("arguments", {}).get("lead_id"),
            "task_id": (payload or {}).get("id") if action == "create_task" else None,
        }
        results.append(item)
        if action == "create_lead" and payload:
            created_id = payload.get("id")
            created_name = payload.get("name")

    message = _friendly_summary(results, failures)
    payload = {"results": results, "failures": failures, "message": message}
    if results and not failures:
        audit.mark_status(row["id"], user_id, "executed", result=payload, executed=True)
        return {"ok": True, "status": "executed", "results": results, "message": message}, 200
    if results and failures:
        audit.mark_status(row["id"], user_id, "partial", result=payload, executed=True)
        return {
            "ok": False,
            "status": "partial",
            "results": results,
            "failures": failures,
            "message": message,
        }, 200
    audit.mark_status(
        row["id"],
        user_id,
        "failed",
        result=payload,
        executed=True,
    )
    first = failures[0] if failures else {}
    body = {"ok": False, "error": first.get("error") or "Could not complete that action.", "choices": first.get("choices") or [], "completed": results}
    return body, 409 if first.get("choices") else 400
