"""Calendar Connections: OAuth connect/callback/disconnect.

Mirrors the social-connections pattern (social_routes.py). OAuth state
tokens (calendar_connections_db.create_oauth_state/consume_oauth_state)
provide CSRF protection across the authorize -> callback round trip;
the callback always consumes (single-use) the state before doing
anything else. Google is the first provider (Phase III-4); Microsoft
is second behind the same provider interface and is not built yet.
"""

from __future__ import annotations

import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import auth
import config
import calendar_connections_db as cal_db
from calendar_providers.base import CalendarProviderError
from calendar_providers.registry import all_providers, get_provider, readiness_for

logger = logging.getLogger(__name__)

calendar_bp = Blueprint("calendar", __name__)


def _auth_gate():
    user = auth.get_current_user()
    if not user:
        nxt = request.path
        if request.query_string:
            nxt = f"{request.path}?{request.query_string.decode()}"
        return None, redirect(url_for("login", next=nxt))
    if config.SUBSCRIPTION_REQUIRED and not auth.user_has_active_subscription(user):
        return None, redirect(url_for("subscribe"))
    return user, None


def _callback_url(provider_name: str) -> str:
    return f"{config.APP_BASE_URL}/calendar/connections/{provider_name}/callback"


@calendar_bp.route("/calendar/connections", methods=["GET"])
def calendar_connections_page():
    user, resp = _auth_gate()
    if resp:
        return resp
    connections = cal_db.list_connections(user["id"])
    by_provider: dict = {}
    for conn in connections:
        by_provider.setdefault(conn["provider"], []).append(conn)
    providers_view = []
    for provider in all_providers():
        providers_view.append(
            {
                "name": provider.name,
                "display_name": provider.display_name,
                "readiness": readiness_for(provider.name),
                "connections": by_provider.get(provider.name, []),
            }
        )
    return render_template(
        "calendar_connections.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="calendar-connections",
        product_name=config.PRODUCT_NAME,
        providers=providers_view,
    )


@calendar_bp.route("/calendar/connections/<provider_name>/connect", methods=["GET"])
def calendar_connect(provider_name):
    user, resp = _auth_gate()
    if resp:
        return resp
    provider = get_provider(provider_name)
    if not provider:
        flash("Unknown calendar provider.", "error")
        return redirect(url_for("calendar.calendar_connections_page"))
    readiness = readiness_for(provider_name)
    if not readiness.get("ready"):
        flash(readiness.get("reason") or f"{provider.display_name} isn't available yet.", "error")
        return redirect(url_for("calendar.calendar_connections_page"))
    redirect_uri = _callback_url(provider_name)
    state = cal_db.create_oauth_state(user["id"], provider_name, redirect_uri=redirect_uri)
    return redirect(provider.get_authorize_url(state=state, redirect_uri=redirect_uri))


@calendar_bp.route("/calendar/connections/<provider_name>/callback", methods=["GET"])
def calendar_callback(provider_name):
    user, resp = _auth_gate()
    if resp:
        return resp
    provider = get_provider(provider_name)
    if not provider:
        flash("Unknown calendar provider.", "error")
        return redirect(url_for("calendar.calendar_connections_page"))

    error = request.args.get("error")
    if error:
        logger.info("%s calendar OAuth callback returned error=%s", provider_name, error)
        flash(f"{provider.display_name} connection was cancelled or denied.", "error")
        return redirect(url_for("calendar.calendar_connections_page"))

    state = request.args.get("state")
    state_row = cal_db.consume_oauth_state(state, provider_name)
    if not state_row or state_row.get("user_id") != user["id"]:
        flash("That connection link expired or was already used. Please try connecting again.", "error")
        return redirect(url_for("calendar.calendar_connections_page"))

    code = request.args.get("code")
    if not code:
        flash(f"{provider.display_name} didn't return an authorization code.", "error")
        return redirect(url_for("calendar.calendar_connections_page"))

    try:
        identity = provider.exchange_code(code=code, redirect_uri=state_row.get("redirect_uri"))
    except CalendarProviderError as exc:
        logger.warning("%s calendar OAuth exchange failed: %s", provider_name, exc)
        flash(exc.user_message, "error")
        return redirect(url_for("calendar.calendar_connections_page"))
    except Exception:
        logger.exception("Unexpected error completing %s calendar OAuth", provider_name)
        flash(f"Couldn't connect {provider.display_name}. Please try again.", "error")
        return redirect(url_for("calendar.calendar_connections_page"))

    cal_db.upsert_connection(
        user["id"],
        provider_name,
        external_account_id=identity["external_account_id"],
        display_name=identity.get("display_name"),
        access_token=identity.get("access_token"),
        refresh_token=identity.get("refresh_token"),
        token_expires_at=identity.get("expires_at"),
        scopes=identity.get("scopes"),
        calendar_id=identity.get("calendar_id"),
    )
    flash(f"{provider.display_name} connected.", "success")
    return redirect(url_for("calendar.calendar_connections_page"))


@calendar_bp.route("/calendar/connections/<int:connection_id>/disconnect", methods=["POST"])
def calendar_disconnect(connection_id):
    user, resp = _auth_gate()
    if resp:
        return resp
    cal_db.disconnect(user["id"], connection_id)
    flash("Disconnected.", "success")
    return redirect(url_for("calendar.calendar_connections_page"))


@calendar_bp.route("/api/calendar/connections", methods=["GET"])
def calendar_connections_json():
    """Lightweight summary for the inbox confirm card: whether a
    calendar is connected (the confirm action requires one)."""
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    active = cal_db.get_active_connection(user["id"])
    return jsonify(
        {
            "connected": active is not None,
            "provider": (active or {}).get("provider"),
            "display_name": (active or {}).get("display_name"),
        }
    )
