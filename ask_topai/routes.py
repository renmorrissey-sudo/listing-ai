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
    # Never accept model JSON or tool names from the client.
    result = service.interpret(user["id"], transcript, context)
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
