"""Ask TopAI HTTP API. Authenticated, tenant-scoped, no secret exposure."""

import logging

from flask import Blueprint, Response, jsonify, request

import auth
from ask_topai import service

logger = logging.getLogger(__name__)

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


def _live_error_payload(exc, extra=None):
    from ask_topai.realtime import settings as live_settings

    payload = {
        "ok": False,
        "error": getattr(exc, "user_message", None) or str(exc),
        "code": getattr(exc, "code", "error"),
        "stage": getattr(exc, "stage", None),
        "ref": getattr(exc, "ref", None),
        "openai_status": getattr(exc, "openai_status", None),
        "openai_api_key_present": live_settings.key_present(),
        "model": live_settings.realtime_model(),
    }
    if extra:
        payload.update(extra)
    return payload


@ask_topai_bp.route("/api/ask-topai/live/session", methods=["POST"], strict_slashes=False)
@auth.subscription_required
def api_live_session():
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.realtime import service as live_service

    data = request.get_json(silent=True) or {}
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    session_id = str(data.get("session_id") or "").strip() or None
    return jsonify(live_service.start_session(user["id"], context, session_id))


@ask_topai_bp.route("/api/ask-topai/live/webrtc", methods=["POST"], strict_slashes=False)
@auth.subscription_required
def api_live_webrtc():
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.realtime import service as live_service
    from ask_topai.realtime.openai_client import RealtimeSessionError, looks_like_html, looks_like_sdp

    ctype = ((request.content_type or "").split(";")[0] or "").strip().lower()
    if ctype in {"application/json", "text/html", "application/xml"}:
        return jsonify(
            {
                "ok": False,
                "error": "TopAI could not establish the Realtime session.",
                "code": "unsupported_content_type",
                "stage": "backend_webrtc",
            }
        ), 415
    raw = request.get_data(as_text=True) or ""
    if looks_like_html(raw) or not looks_like_sdp(raw):
        return jsonify(
            {
                "ok": False,
                "error": "TopAI could not establish the Realtime session.",
                "code": "invalid_offer",
                "stage": "invalid_offer",
            }
        ), 400
    session_id = (
        (request.headers.get("X-Ask-TopAI-Session") or request.args.get("session_id") or "")
        .strip()
        or None
    )
    page = (request.headers.get("X-Ask-TopAI-Page") or request.args.get("page") or "").strip()
    lead_raw = (request.headers.get("X-Ask-TopAI-Lead-Id") or request.args.get("lead_id") or "").strip()
    lead_id = None
    if lead_raw:
        try:
            lead_id = int(lead_raw)
        except ValueError:
            lead_id = lead_raw
    context = {"page": page, "lead_id": lead_id}
    try:
        result = live_service.start_webrtc(user["id"], raw, context, session_id)
    except RealtimeSessionError as exc:
        return jsonify(_live_error_payload(exc)), exc.http_status
    except Exception:
        from ask_topai.realtime.openai_client import new_ref

        ref = new_ref()
        logger.exception("Ask TopAI realtime webrtc unexpected error ref=%s", ref)
        return jsonify(
            {
                "ok": False,
                "error": "TopAI could not establish the Realtime session.",
                "code": "error",
                "stage": "backend_webrtc",
                "ref": ref,
            }
        ), 503
    response = Response(result["sdp"], status=200, mimetype="application/sdp")
    response.headers["X-Ask-TopAI-Session-Id"] = result["session_id"]
    response.headers["X-Ask-TopAI-Ref"] = result["ref"]
    response.headers["Cache-Control"] = "no-store"
    return response


@ask_topai_bp.route("/api/ask-topai/live/diagnostics", methods=["GET"], strict_slashes=False)
@auth.subscription_required
def api_live_diagnostics():
    user, err = _user_or_401()
    if err:
        return err
    from ask_topai.realtime import service as live_service

    probe = str(request.args.get("probe") or "").strip().lower()
    body = live_service.diagnostics(user_id=user["id"], probe_calls=probe in {"calls", "1", "true", "sdp"})
    return jsonify(body), 200 if body.get("ok") else 503


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
