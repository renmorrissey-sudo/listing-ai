"""Ask TopAI HTTP API. Authenticated, tenant-scoped, no secret exposure."""

from flask import Blueprint, jsonify, request

import auth
from ask_topai import service

ask_topai_bp = Blueprint("ask_topai", __name__)


def _user_or_401():
    user = auth.get_current_user()
    if not user:
        return None, (jsonify({"error": "Please log in to continue."}), 401)
    return user, None


@ask_topai_bp.route("/api/ask-topai/interpret", methods=["POST"])
@auth.subscription_required
def api_interpret():
    user, err = _user_or_401()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    transcript = str(data.get("text") or data.get("transcript") or "").strip()
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    session_id = str(data.get("session_id") or "").strip() or None
    source = str(data.get("source") or "text").strip().lower()
    request_id = str(data.get("request_id") or "").strip() or None
    # Never accept model JSON or tool names from the client.
    result = service.interpret(
        user["id"],
        transcript,
        context,
        session_id=session_id,
        source=source,
        request_id=request_id,
    )
    return jsonify(result)


@ask_topai_bp.route("/api/ask-topai/confirm", methods=["POST"])
@auth.subscription_required
def api_confirm():
    user, err = _user_or_401()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    token = str(data.get("confirmation_token") or "").strip()
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    result, status = service.confirm(user["id"], token, context)
    return jsonify(result), status


@ask_topai_bp.route("/api/ask-topai/cancel", methods=["POST"])
@auth.subscription_required
def api_cancel():
    user, err = _user_or_401()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    token = str(data.get("confirmation_token") or "").strip()
    pending = None
    if token:
        from ask_topai import audit

        pending = audit.get_pending_by_token(user["id"], token)
        if pending:
            audit.mark_status(pending["id"], user["id"], "cancelled")
    return jsonify({"ok": True, "status": "cancelled"})


@ask_topai_bp.route("/api/ask-topai/live/health", methods=["GET"])
@auth.subscription_required
def api_live_health():
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.realtime import service as live_service

    body = live_service.health()
    return jsonify(body), 200 if body.get("ok") else 503


@ask_topai_bp.route("/api/ask-topai/live/session", methods=["POST"])
@auth.subscription_required
def api_live_session():
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.realtime import service as live_service
    from ask_topai.realtime.openai_client import RealtimeSessionError

    data = request.get_json(silent=True) or {}
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    session_id = str(data.get("session_id") or "").strip() or None
    try:
        result = live_service.start_session(user["id"], context, session_id)
    except RealtimeSessionError as exc:
        return jsonify(
            {
                "ok": False,
                "error": exc.user_message,
                "code": exc.code,
                "openai_configured": False if exc.code == "not_configured" else None,
            }
        ), exc.http_status
    return jsonify(result)


@ask_topai_bp.route("/api/ask-topai/live/tools", methods=["POST"])
@auth.subscription_required
def api_live_tools():
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.realtime import runtime

    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"ok": False, "error": "A live session is required."}), 400
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    transcript = str(data.get("transcript") or data.get("utterance") or "")[:2000]
    calls = data.get("calls")
    if not isinstance(calls, list):
        if data.get("name") and data.get("call_id"):
            calls = [
                {
                    "name": data.get("name"),
                    "call_id": data.get("call_id"),
                    "arguments": data.get("arguments"),
                }
            ]
        else:
            calls = []
    if not calls:
        return jsonify({"ok": False, "error": "No tool calls were provided."}), 400
    result = runtime.execute_calls(
        user["id"],
        session_id,
        calls,
        context,
        transcript=transcript,
    )
    result["ok"] = True
    return jsonify(result)


@ask_topai_bp.route("/api/ask-topai/live/end", methods=["POST"])
@auth.subscription_required
def api_live_end():
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.realtime import service as live_service

    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id") or "").strip() or None
    transcript = data.get("transcript") if isinstance(data.get("transcript"), list) else []
    return jsonify(live_service.end_session(user["id"], session_id, transcript))


@ask_topai_bp.route("/api/ask-topai/health", methods=["GET"])
@auth.subscription_required
def api_health():
    """Authenticated Claude connectivity check. No CRM mutation. No secrets."""
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.claude import ping

    result = ping()
    body = {
        "ok": bool(result.get("ok")),
        "status": result.get("code") or "error",
        "message": result.get("message"),
        "model": result.get("model"),
    }
    return jsonify(body), 200 if body["ok"] else 503
