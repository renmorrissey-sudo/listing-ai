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


def _api_status(internal: str) -> str:
    return {
        "ok": "executed",
        "needs_clarification": "clarification_required",
        "unsupported": "unsupported_action",
        "informational": "informational_response",
        "error": "error",
    }.get(internal, internal)


def _audit_kwargs(raw, session_id=None, request_id=None):
    return {
        "model": raw.get("model"),
        "input_source": raw.get("source"),
        "tools_invoked": raw.get("tools_invoked"),
        "session_key": raw.get("session_id") or session_id,
        "request_id": request_id,
    }


def _refresh_hints(results: list) -> dict:
    hints = {"leads": False, "tasks": False, "lead_ids": []}
    for item in results or []:
        action = item.get("action")
        if action == "create_lead":
            hints["leads"] = True
        if action == "create_task":
            hints["tasks"] = True
        if action in {"add_lead_note", "update_property_criteria", "create_lead"}:
            hints["leads"] = True
        lead_id = item.get("lead_id")
        if lead_id:
            hints["lead_ids"].append(lead_id)
    return hints


def _replay_request(row: dict, session_id=None) -> dict:
    try:
        previous = json.loads(row.get("result_json") or "{}")
    except json.JSONDecodeError:
        previous = {}
    status = row.get("status")
    if status == "in_progress":
        return {
            "ok": True,
            "status": "working",
            "message": "Ask TopAI is working...",
            "duplicate": True,
            "preview": None,
            "confirmation_token": None,
            "choices": [],
            "session_id": session_id or row.get("session_key"),
        }
    return {
        "ok": True,
        "status": status if status in {"executed", "partial"} else status,
        "message": previous.get("message") or "This request was already completed.",
        "results": previous.get("results") or [],
        "failures": previous.get("failures") or [],
        "duplicate": True,
        "preview": None,
        "confirmation_token": None,
        "choices": [],
        "session_id": session_id or row.get("session_key"),
        "refresh": previous.get("refresh") or _refresh_hints(previous.get("results") or []),
    }


def interpret(
    user_id,
    transcript: str,
    raw_context: dict | None,
    *,
    session_id=None,
    source="text",
    request_id=None,
):
    context = sanitize_context(user_id, raw_context)
    source = source if source in {"voice", "text"} else "text"
    request_id = str(request_id or "").strip()[:80] or None
    reuse_id = None
    if request_id:
        existing = audit.get_by_request_id(user_id, request_id)
        if existing:
            status_existing = existing.get("status")
            if status_existing in {"executed", "partial"}:
                return _replay_request(existing, session_id)
            if status_existing == "in_progress":
                from datetime import datetime, timedelta, timezone

                created = existing.get("created_at") or existing.get("updated_at") or ""
                stale = True
                try:
                    ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    stale = datetime.now(timezone.utc) - ts > timedelta(minutes=2)
                except (TypeError, ValueError):
                    stale = True
                if not stale:
                    return _replay_request(existing, session_id)
                reuse_id = existing["id"]
            else:
                reuse_id = existing["id"]

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
    audit_kw = _audit_kwargs(raw, session_id, None)
    audit_kw_id = _audit_kwargs(raw, session_id, request_id)

    if raw.get("status") == "error":
        audit.record_command(
            user_id,
            transcript=transcript,
            interpreted={"status": "error", "message": raw.get("message"), "code": raw.get("code")},
            status="error",
            lead_id=lead_id,
            result={"message": raw.get("message"), "code": raw.get("code")},
            **audit_kw,
        )
        return {
            "ok": True,
            "status": "error",
            "code": raw.get("code"),
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
            **audit_kw,
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
            **audit_kw,
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

    target_lead = None
    for command in resolved:
        if command.get("arguments", {}).get("lead_id"):
            target_lead = command["arguments"]["lead_id"]
            break
    if reuse_id:
        audit.mark_status(reuse_id, user_id, "in_progress")
        row_id = reuse_id
    else:
        row_id = audit.record_command(
            user_id,
            transcript=transcript,
            interpreted={
                "status": "ok",
                "commands": resolved,
                "grounding_transcript": grounding,
            },
            status="in_progress",
            lead_id=target_lead,
            **audit_kw_id,
        )
    results, failures, message = _run_commands(user_id, resolved, grounding, context)
    refresh = _refresh_hints(results)
    payload = {"results": results, "failures": failures, "message": message, "refresh": refresh}
    if results and not failures:
        audit.mark_status(row_id, user_id, "executed", result=payload, executed=True)
        return {
            "ok": True,
            "status": "executed",
            "message": message,
            "results": results,
            "preview": None,
            "confirmation_token": None,
            "choices": [],
            "session_id": session_id,
            "refresh": refresh,
        }
    if results and failures:
        audit.mark_status(row_id, user_id, "partial", result=payload, executed=True)
        return {
            "ok": False,
            "status": "partial",
            "message": message,
            "results": results,
            "failures": failures,
            "preview": None,
            "confirmation_token": None,
            "choices": [],
            "session_id": session_id,
            "refresh": refresh,
        }
    audit.mark_status(row_id, user_id, "failed", result=payload, executed=True)
    first = failures[0] if failures else {}
    return {
        "ok": False,
        "status": "error",
        "message": first.get("error") or message or "Could not complete that action.",
        "choices": first.get("choices") or [],
        "results": results,
        "session_id": session_id,
        "confirmation_token": None,
        "preview": None,
    }


def _run_commands(user_id, commands: list, grounding: str, context: dict):
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
    return results, failures, _friendly_summary(results, failures)


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

    results, failures, message = _run_commands(user_id, commands, grounding, context)
    payload = {"results": results, "failures": failures, "message": message, "refresh": _refresh_hints(results)}
    if results and not failures:
        audit.mark_status(row["id"], user_id, "executed", result=payload, executed=True)
        return {"ok": True, "status": "executed", "results": results, "message": message, "refresh": payload["refresh"]}, 200
    if results and failures:
        audit.mark_status(row["id"], user_id, "partial", result=payload, executed=True)
        return {
            "ok": False,
            "status": "partial",
            "results": results,
            "failures": failures,
            "message": message,
            "refresh": payload["refresh"],
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
