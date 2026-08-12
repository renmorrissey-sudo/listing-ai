"""Social Media Connections: OAuth connect/callback/disconnect + default channels.

Mirrors the auth-gate pattern used by sms_campaigns.py. OAuth state tokens
(social_connections_db.create_oauth_state/consume_oauth_state) provide CSRF
protection across the authorize -> callback round trip; the callback always
consumes (single-use) the state before doing anything else.
"""

from __future__ import annotations

import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import auth
import config
import social_connections_db as social_db
from social_providers.base import SocialProviderError
from social_providers.registry import all_providers, get_provider, readiness_for

logger = logging.getLogger(__name__)

social_bp = Blueprint("social", __name__)


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
    return f"{config.APP_BASE_URL}/social/connections/{provider_name}/callback"


@social_bp.route("/social/connections", methods=["GET"])
def social_connections_page():
    user, resp = _auth_gate()
    if resp:
        return resp
    connections = social_db.list_connections(user["id"])
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
        "social_connections.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="social-connections",
        product_name=config.PRODUCT_NAME,
        providers=providers_view,
    )


@social_bp.route("/social/default-channels", methods=["GET"])
def social_default_channels_json():
    """Lightweight summary for the Listing Generator's Post button: which
    connected+enabled channels a one-click Post will actually reach."""
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    from listing_publish import default_target_connections

    channels = default_target_connections(user["id"])
    return jsonify({
        "channels": [
            {"provider": c["provider"], "display_name": c.get("display_name")} for c in channels
        ]
    })


@social_bp.route("/social/connections/<provider_name>/connect", methods=["GET"])
def social_connect(provider_name):
    user, resp = _auth_gate()
    if resp:
        return resp
    provider = get_provider(provider_name)
    if not provider:
        flash("Unknown social provider.", "error")
        return redirect(url_for("social.social_connections_page"))
    readiness = readiness_for(provider_name)
    if not readiness.get("ready"):
        flash(readiness.get("reason") or f"{provider.display_name} isn't available yet.", "error")
        return redirect(url_for("social.social_connections_page"))
    redirect_uri = _callback_url(provider_name)
    state = social_db.create_oauth_state(user["id"], provider_name, redirect_uri=redirect_uri)
    return redirect(provider.get_authorize_url(state=state, redirect_uri=redirect_uri))


@social_bp.route("/social/connections/<provider_name>/callback", methods=["GET"])
def social_callback(provider_name):
    user, resp = _auth_gate()
    if resp:
        return resp
    provider = get_provider(provider_name)
    if not provider:
        flash("Unknown social provider.", "error")
        return redirect(url_for("social.social_connections_page"))

    error = request.args.get("error")
    if error:
        logger.info("%s OAuth callback returned error=%s", provider_name, error)
        flash(f"{provider.display_name} connection was cancelled or denied.", "error")
        return redirect(url_for("social.social_connections_page"))

    state = request.args.get("state")
    state_row = social_db.consume_oauth_state(state, provider_name)
    if not state_row or state_row.get("user_id") != user["id"]:
        flash("That connection link expired or was already used. Please try connecting again.", "error")
        return redirect(url_for("social.social_connections_page"))

    code = request.args.get("code")
    if not code:
        flash(f"{provider.display_name} didn't return an authorization code.", "error")
        return redirect(url_for("social.social_connections_page"))

    try:
        identity = provider.exchange_code(code=code, redirect_uri=state_row.get("redirect_uri"))
    except SocialProviderError as exc:
        logger.warning("%s OAuth exchange failed: %s", provider_name, exc)
        flash(exc.user_message, "error")
        return redirect(url_for("social.social_connections_page"))
    except Exception:
        logger.exception("Unexpected error completing %s OAuth", provider_name)
        flash(f"Couldn't connect {provider.display_name}. Please try again.", "error")
        return redirect(url_for("social.social_connections_page"))

    social_db.upsert_connection(
        user["id"],
        provider_name,
        external_account_id=identity["external_account_id"],
        display_name=identity.get("display_name"),
        access_token=identity.get("access_token"),
        refresh_token=identity.get("refresh_token"),
        token_expires_at=identity.get("expires_at"),
        scopes=identity.get("scopes"),
    )
    flash(f"{provider.display_name} connected.", "success")
    return redirect(url_for("social.social_connections_page"))


@social_bp.route("/social/connections/<int:connection_id>/disconnect", methods=["POST"])
def social_disconnect(connection_id):
    user, resp = _auth_gate()
    if resp:
        return resp
    social_db.disconnect(user["id"], connection_id)
    flash("Disconnected.", "success")
    return redirect(url_for("social.social_connections_page"))


@social_bp.route("/social/connections/default-channels", methods=["POST"])
def social_default_channels():
    user, resp = _auth_gate()
    if resp:
        return resp
    enabled_ids = {int(v) for v in request.form.getlist("enabled_connection_ids") if v.isdigit()}
    for conn in social_db.list_connections(user["id"]):
        if conn["status"] != "connected":
            continue
        social_db.set_default_enabled(user["id"], conn["id"], conn["id"] in enabled_ids)
    flash("Default posting channels saved.", "success")
    return redirect(url_for("social.social_connections_page"))
