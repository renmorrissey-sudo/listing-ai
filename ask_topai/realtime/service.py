"""Start/end Ask TopAI live realtime sessions. Audio never transits this module."""

from __future__ import annotations

from ask_topai import audit, registry, sessions
from ask_topai.realtime import instructions, openai_client, settings, store
from ask_topai.service import sanitize_context


def openai_tools() -> list[dict]:
    """Realtime function tools from the model-independent registry."""
    return registry.openai_tools()


def start_session(user_id, raw_context: dict | None, session_id: str | None = None):
    context = sanitize_context(user_id, raw_context)
    key = (session_id or "").strip() or sessions.create_session_key()
    existing = sessions.get_session(user_id, key)
    state = store.get_state(user_id, key) if existing else {}
    state = dict(state or {})
    state["mode"] = "live"
    state["model"] = settings.realtime_model()
    state["provider"] = settings.provider_name()
    state["context"] = context
    prompt = instructions.build_instructions(context, store.completed_actions(state))
    session_obj = settings.session_config(prompt, openai_tools())
    secret = openai_client.mint_ephemeral_secret(session_obj, user_id=user_id)
    store.save_state(user_id, key, state, status=store.LIVE_STATUS)
    public = settings.public_client_config()
    return {
        "ok": True,
        "session_id": key,
        "client_secret": {"value": secret["value"], "expires_at": secret.get("expires_at")},
        "model": public["model"],
        "provider": public["provider"],
        "calls_url": public["calls_url"],
        "ice_servers": public["ice_servers"],
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
        "message": None
        if configured
        else (
            "Ask TopAI Live Conversation is not configured yet."
            if not present
            else "Ask TopAI Live Conversation is not ready."
        ),
    }
