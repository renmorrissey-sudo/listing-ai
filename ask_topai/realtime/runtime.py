"""Execute Ask TopAI tools for a live Realtime session. Model-independent."""

from __future__ import annotations

import hashlib
import json
import logging

from ask_topai import audit, policy, registry, tools
from ask_topai.realtime import store
from ask_topai.schemas import sanitize_command
from ask_topai.service import _bind_created_lead, _refresh_hints, _run_commands, sanitize_context

logger = logging.getLogger(__name__)

WRITE_ORDER = {
    "find_lead": 0,
    "get_lead_context": 1,
    "list_lead_tasks": 1,
    "get_calendar_availability": 1,
    "find_available_slots": 1,
    "get_existing_appointment": 1,
    "create_lead": 2,
    "update_property_criteria": 3,
    "add_lead_note": 3,
    "update_lead_status": 3,
    "create_task": 4,
    "create_follow_up": 4,
    "create_calendar_event": 5,
    "reschedule_calendar_event": 5,
}


def parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return dict(data) if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def action_id_for(session_key: str, name: str, arguments: dict) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    raw = f"{session_key}:{name}:{canonical}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _public_output(payload: dict) -> dict:
    """JSON the realtime model receives. No secrets."""
    return payload


def _replay(row: dict) -> dict:
    output = store.invocation_output(row) or {}
    output.setdefault("duplicate", True)
    output.setdefault("ok", bool(output.get("ok", True)))
    return output


def _confirmation_required(name: str) -> dict:
    return {
        "ok": False,
        "status": "confirmation_required",
        "confirmation_policy": "spoken_confirmation",
        "tool": name,
        "message": (
            "That action is not enabled yet. When it is, Ask TopAI will ask "
            "for an explicit confirmation before doing it."
        ),
        "executed": False,
    }


def _execute_write(user_id, name, arguments, context, transcript, created_lead):
    command = {"action": name, "arguments": dict(arguments or {})}
    _bind_created_lead(
        command,
        (created_lead or {}).get("id"),
        (created_lead or {}).get("name"),
    )
    results, failures, message = _run_commands(user_id, [command], transcript, context)
    if results and not failures:
        item = results[0]
        return {
            "ok": True,
            "status": "executed",
            "tool": name,
            "message": item.get("message") or message,
            "lead_id": item.get("lead_id"),
            "task_id": item.get("task_id"),
            "results": results,
            "refresh": _refresh_hints(results),
        }
    if failures:
        first = failures[0]
        return {
            "ok": False,
            "status": "error",
            "tool": name,
            "message": first.get("error") or message or "That action could not be completed.",
            "choices": first.get("choices") or [],
            "results": results,
            "failures": failures,
        }
    return {
        "ok": False,
        "status": "error",
        "tool": name,
        "message": message or "That action could not be completed.",
    }


def execute_call(user_id, session_key, call, context, state, transcript=""):
    name = str((call or {}).get("name") or "").strip()
    call_id = str((call or {}).get("call_id") or "").strip() or action_id_for(
        session_key, name or "unknown", {}
    )
    arguments = parse_arguments((call or {}).get("arguments"))
    mode = policy.confirmation_mode(name)

    existing = store.get_invocation(user_id, session_key, call_id=call_id)
    if existing and existing.get("status") in {"executed", "error", "blocked"}:
        return _replay(existing), state

    if mode == policy.MODE_FORBIDDEN or not name:
        result = {
            "ok": False,
            "status": "forbidden",
            "tool": name or None,
            "message": "That action is not available.",
            "executed": False,
        }
        store.record_invocation(
            user_id,
            session_key,
            call_id=call_id,
            action_id=action_id_for(session_key, name or "forbidden", arguments),
            tool_name=name or "unknown",
            arguments=arguments,
            result=result,
            status="blocked",
        )
        return result, state

    if mode == policy.MODE_SPOKEN_CONFIRMATION:
        result = _confirmation_required(name)
        store.record_invocation(
            user_id,
            session_key,
            call_id=call_id,
            action_id=action_id_for(session_key, name, arguments),
            tool_name=name,
            arguments=arguments,
            result=result,
            status="blocked",
        )
        return result, state

    fingerprint_args = arguments
    if registry.is_write_tool(name):
        cleaned, _err = sanitize_command({"action": name, "arguments": arguments}, transcript)
        if cleaned:
            fingerprint_args = cleaned.get("arguments") or arguments

    action_id = action_id_for(session_key, name, fingerprint_args)
    claimed, is_new = store.claim_invocation(
        user_id,
        session_key,
        call_id=call_id,
        action_id=action_id,
        tool_name=name,
        arguments=fingerprint_args,
    )
    if claimed and not is_new:
        if claimed.get("status") in {"executed", "error", "blocked"}:
            return _replay(claimed), state
        return {
            "ok": True,
            "status": "working",
            "duplicate": True,
            "message": "Ask TopAI is already completing that action.",
            "executed": False,
        }, state

    if registry.is_read_tool(name):
        payload = tools.dispatch_read(name, arguments, user_id, context)
        ok = not (isinstance(payload, dict) and payload.get("error"))
        result = {
            "ok": ok,
            "status": "ok" if ok else "error",
            "tool": name,
            "message": None if ok else payload.get("error"),
            **(payload if isinstance(payload, dict) else {"data": payload}),
        }
        store.complete_invocation(
            user_id,
            session_key,
            call_id,
            result=result,
            status="executed" if ok else "error",
            lead_id=(payload or {}).get("id") if isinstance(payload, dict) else None,
        )
        audit.record_command(
            user_id,
            transcript=transcript or f"live:{name}",
            interpreted={"tool": name, "arguments": arguments, "source": "live"},
            status="executed" if ok else "error",
            lead_id=(payload or {}).get("id") if isinstance(payload, dict) else context.get("lead_id"),
            result=result,
            model=state.get("model"),
            input_source="live",
            tools_invoked=[name],
            session_key=session_key,
            request_id=call_id,
        )
        return _public_output(result), state

    created = store.last_created_lead(state)
    result = _execute_write(user_id, name, arguments, context, transcript, created)
    lead_id = result.get("lead_id") or context.get("lead_id")
    status = "executed" if result.get("ok") else "error"
    store.complete_invocation(
        user_id,
        session_key,
        call_id,
        result=result,
        status=status,
        lead_id=lead_id,
    )
    if result.get("ok"):
        lead_name = None
        for item in result.get("results") or []:
            if item.get("action") == "create_lead":
                lead_name = (item.get("message") or "").replace("Lead created: ", "").strip() or None
        state = store.remember_action(
            state,
            {
                "tool": name,
                "summary": result.get("message"),
                "lead_id": lead_id,
                "lead_name": lead_name,
                "call_id": call_id,
            },
        )
    audit.record_command(
        user_id,
        transcript=transcript or f"live:{name}",
        interpreted={"tool": name, "arguments": fingerprint_args, "source": "live"},
        status=status,
        lead_id=lead_id,
        result=result,
        model=state.get("model"),
        input_source="live",
        tools_invoked=[name],
        session_key=session_key,
        request_id=call_id,
    )
    return _public_output(result), state


def execute_calls(user_id, session_key, calls, raw_context, *, transcript=""):
    context = sanitize_context(user_id, raw_context)
    state = store.get_state(user_id, session_key) or {}
    ordered = sorted(
        [c for c in (calls or []) if isinstance(c, dict)],
        key=lambda item: WRITE_ORDER.get(str(item.get("name") or ""), 50),
    )
    outputs = []
    refresh = {"leads": False, "tasks": False, "lead_ids": []}
    for call in ordered:
        result, state = execute_call(user_id, session_key, call, context, state, transcript)
        outputs.append(
            {
                "call_id": str(call.get("call_id") or ""),
                "name": str(call.get("name") or ""),
                "output": result,
            }
        )
        extra = result.get("refresh") if isinstance(result, dict) else None
        if extra:
            refresh["leads"] = refresh["leads"] or bool(extra.get("leads"))
            refresh["tasks"] = refresh["tasks"] or bool(extra.get("tasks"))
            refresh["lead_ids"].extend(extra.get("lead_ids") or [])
            if result.get("lead_id"):
                refresh["lead_ids"].append(result["lead_id"])
    store.save_state(user_id, session_key, state, status=store.LIVE_STATUS)
    refresh["lead_ids"] = list(dict.fromkeys([x for x in refresh["lead_ids"] if x]))
    return {"results": outputs, "refresh": refresh, "session_id": session_key}
