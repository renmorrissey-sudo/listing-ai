"""Start/end Ask TopAI live realtime sessions. Audio never transits this module."""

from __future__ import annotations

from ask_topai import audit, registry, sessions
from ask_topai.realtime import instructions, openai_client, settings, store
from ask_topai.service import sanitize_context


def openai_tools() -> list[dict]:
    """Realtime function tools from the model-independent registry."""
    return registry.openai_tools()


def public_tool_specs() -> list[dict]:
    """JSON tool specs for the browser Agents SDK. No secrets."""
    specs = []
    for item in openai_tools():
        specs.append(
            {
                "name": item.get("name"),
                "description": item.get("description") or "",
                "parameters": item.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return specs


def _prepare_session(user_id, raw_context: dict | None, session_id: str | None = None):
    context = sanitize_context(user_id, raw_context)
    key = (session_id or "").strip() or sessions.create_session_key()
    existing = sessions.get_session(user_id, key)
    state = store.get_state(user_id, key) if existing else {}
    state = dict(state or {})
    state["mode"] = "live"
    state["model"] = settings.realtime_model()
    state["provider"] = settings.provider_name()
    state["context"] = context
    store.save_state(user_id, key, state, status=store.LIVE_STATUS)
    prompt = instructions.build_instructions(context, store.completed_actions(state))
    session_obj = settings.session_config(prompt, openai_tools())
    return key, context, session_obj, prompt


def start_session(user_id, raw_context: dict | None, session_id: str | None = None):
    """Mint an ephemeral Realtime client secret. Permanent OPENAI_API_KEY stays here."""
    key, context, session_obj, prompt = _prepare_session(user_id, raw_context, session_id)
    secret = openai_client.mint_ephemeral_secret(session_obj, user_id=user_id)
    public = settings.public_client_config()
    return {
        "ok": True,
        "session_id": key,
        "client_secret": {"value": secret["value"], "expires_at": secret.get("expires_at")},
        "model": public["model"],
        "voice": public["voice"],
        "provider": public["provider"],
        "instructions": prompt,
        "tools": public_tool_specs(),
        "ref": secret.get("ref"),
        "openai_configured": True,
        "openai_api_key_present": True,
        "context": context,
    }


def end_session(user_id, session_id: str | None, transcript=None):
    key = (session_id or "").strip()
    if not key:
        return {"ok": True, "status": "ended"}
    state = store.get_state(user_id, key) or {}
    turns = transcript if isinstance(transcript, list) else []
    text = "\n".join(
        f"{item.get('role')}: {item.get('text')}"
        for item in turns
        if isinstance(item, dict) and item.get("text")
    )[:4000]
    context = state.get("context") if isinstance(state.get("context"), dict) else {}
    audit.record_command(
        user_id,
        transcript=text or "live session ended",
        interpreted={
            "event": "live_session_end",
            "turns": turns[-40:],
            "completed_actions": store.completed_actions(state),
        },
        status="executed",
        lead_id=context.get("lead_id"),
        result={"ended": True, "turn_count": len(turns)},
        model=state.get("model") or settings.realtime_model(),
        input_source="live",
        tools_invoked=[item.get("tool") for item in store.completed_actions(state) if item.get("tool")],
        session_key=key,
    )
    sessions.delete_session(user_id, key)
    return {"ok": True, "status": "ended", "session_id": key}


def health() -> dict:
    present = settings.key_present()
    configured = settings.is_configured()
    return {
        "ok": configured,
        "openai_configured": configured,
        "openai_api_key_present": present,
        "realtime_model": settings.realtime_model(),
        "provider": settings.provider_name(),
        "message": None if configured else openai_client.USER_NOT_CONFIGURED,
    }


def diagnostics(*, user_id, probe_secret: bool = False) -> dict:
    """Authenticated OpenAI reachability check. No CRM writes. Never returns secrets."""
    ref = openai_client.new_ref()
    present = settings.key_present()
    configured = settings.is_configured()
    model = settings.realtime_model()
    body = {
        "ok": False,
        "ref": ref,
        "openai_api_key_present": present,
        "openai_configured": configured,
        "realtime_model": model,
        "openai_status": None,
        "request_id": None,
        "openai_authenticated": False,
        "model_listed": None,
        "client_secret_created": False,
        "message": None,
    }
    auth = openai_client.probe_openai_auth(user_id=user_id, ref=ref)
    body.update(
        {
            "openai_status": auth.get("openai_status"),
            "request_id": auth.get("request_id"),
            "openai_authenticated": bool(auth.get("openai_authenticated")),
            "model_listed": auth.get("model_listed"),
            "message": auth.get("message"),
        }
    )
    if not configured:
        body["ok"] = False
        body["message"] = openai_client.USER_NOT_CONFIGURED
        return body
    if probe_secret and body["openai_authenticated"]:
        try:
            minted = openai_client.mint_ephemeral_secret(
                settings.slim_session_config(),
                user_id=user_id,
                ref=ref,
            )
            value = minted.get("value") or ""
            body["client_secret_created"] = bool(value.startswith("ek_"))
            body["client_secret_prefix"] = "ek_" if value.startswith("ek_") else None
            body["openai_status"] = minted.get("openai_status") or body["openai_status"]
            body["ok"] = body["client_secret_created"]
            body["message"] = None if body["ok"] else openai_client.USER_CONNECT
        except openai_client.RealtimeSessionError as exc:
            body["client_secret_created"] = False
            body["openai_status"] = exc.openai_status
            body["message"] = exc.user_message
            body["ok"] = False
        return body
    body["ok"] = bool(body["openai_authenticated"])
    return body
