"""Start/end Ask TopAI live realtime sessions. Audio never transits this module."""

from __future__ import annotations

from ask_topai import audit, registry, sessions
from ask_topai.realtime import instructions, openai_client, settings, store
from ask_topai.service import sanitize_context


def openai_tools() -> list[dict]:
    """Realtime function tools from the model-independent registry."""
    return registry.openai_tools()


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
    return key, context, session_obj


def start_session(user_id, raw_context: dict | None, session_id: str | None = None):
    """Create TopAI live session metadata. Does not call OpenAI or mint secrets."""
    key, context, _session_obj = _prepare_session(user_id, raw_context, session_id)
    public = settings.public_client_config()
    return {
        "ok": True,
        "session_id": key,
        "model": public["model"],
        "provider": public["provider"],
        "webrtc_url": public["webrtc_url"],
        "ice_servers": public["ice_servers"],
        "openai_configured": settings.is_configured(),
        "openai_api_key_present": settings.key_present(),
        "context": context,
    }


def start_webrtc(user_id, sdp: str, raw_context: dict | None, session_id: str | None = None):
    """Handshake: persist TopAI session, POST SDP+session to OpenAI, return SDP answer."""
    key, _context, session_obj = _prepare_session(user_id, raw_context, session_id)
    result = openai_client.create_webrtc_call(sdp, session_obj, user_id=user_id)
    return {
        "sdp": result["sdp"],
        "session_id": key,
        "ref": result["ref"],
        "call_id": result.get("call_id"),
        "openai_status": result.get("openai_status"),
        "model": result.get("model") or settings.realtime_model(),
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
        "webrtc_url": settings.WEBRTC_PATH,
        "message": None
        if configured
        else (
            "Ask TopAI Live Conversation is not configured yet."
            if not present
            else "Ask TopAI Live Conversation is not ready."
        ),
    }


def diagnostics(*, user_id, probe_calls: bool = False) -> dict:
    """Authenticated OpenAI reachability check. No CRM writes."""
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
        "calls_probe": None,
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
    if probe_calls and body["openai_authenticated"]:
        try:
            result = openai_client.create_webrtc_call(
                openai_client.PROBE_SDP,
                settings.slim_session_config(),
                user_id=user_id,
                ref=ref,
            )
            body["calls_probe"] = {
                "ok": True,
                "sdp_answer": True,
                "openai_status": result.get("openai_status"),
            }
            body["ok"] = True
            body["message"] = None
        except openai_client.RealtimeSessionError as exc:
            body["calls_probe"] = {
                "ok": False,
                "sdp_answer": False,
                "openai_status": exc.openai_status,
                "code": exc.code,
                "stage": exc.stage,
            }
            body["message"] = exc.user_message
            body["ok"] = False
        return body
    body["ok"] = bool(body["openai_authenticated"])
    return body
