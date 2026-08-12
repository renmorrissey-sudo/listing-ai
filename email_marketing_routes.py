"""Tenant-owned Email Marketing settings and Listing Generator draft export."""

from __future__ import annotations

import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import auth
import config
import email_marketing_db as marketing_db
from email_campaign_providers.base import EmailCampaignProviderError
from email_campaign_providers.registry import get_provider
from integration_credentials import IntegrationCredentialError, is_configured
from listing_email_campaigns import export_listing_email

logger = logging.getLogger(__name__)

email_marketing_bp = Blueprint("email_marketing", __name__)


def _auth_gate():
    user = auth.get_current_user()
    if not user:
        return None, redirect(url_for("login", next=request.path))
    if config.SUBSCRIPTION_REQUIRED and not auth.user_has_active_subscription(user):
        return None, redirect(url_for("subscribe"))
    return user, None


def _provider_for_user(user_id):
    try:
        credentials = marketing_db.get_credentials(user_id)
    except IntegrationCredentialError:
        logger.exception(
            "Could not decrypt Email Marketing credentials for user_id=%s",
            user_id,
        )
        return None
    if not credentials:
        return None
    return get_provider("sendgrid", api_key=credentials["api_key"])


def _load_resources(user_id):
    provider = _provider_for_user(user_id)
    if not provider:
        return {"senders": [], "lists": [], "suppression_groups": []}, None
    try:
        return provider.test_connection(), None
    except EmailCampaignProviderError as exc:
        return {"senders": [], "lists": [], "suppression_groups": []}, exc.user_message


@email_marketing_bp.route("/integrations/email-marketing", methods=["GET"])
def settings_page():
    user, response = _auth_gate()
    if response:
        return response
    connection = marketing_db.get_connection(user["id"])
    resources, resource_error = _load_resources(user["id"])
    return render_template(
        "email_marketing_settings.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="email-marketing",
        product_name=config.PRODUCT_NAME,
        connection=connection,
        senders=resources.get("senders") or [],
        lists=resources.get("lists") or [],
        suppression_groups=resources.get("suppression_groups") or [],
        resource_error=resource_error,
        encryption_ready=is_configured(),
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/connect", methods=["POST"]
)
def connect():
    user, response = _auth_gate()
    if response:
        return response
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        flash("Enter a SendGrid API key.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    if not is_configured():
        flash(
            "Email Marketing credential encryption is not configured. "
            "Contact support.",
            "error",
        )
        return redirect(url_for("email_marketing.settings_page"))

    try:
        provider = get_provider("sendgrid", api_key=api_key)
        resources = provider.test_connection()
        marketing_db.connect(user["id"], api_key)
        # Safe conveniences only: a sole verified sender and SendGrid's default
        # unsubscribe group. Never choose a recipient list automatically.
        senders = resources.get("senders") or []
        groups = resources.get("suppression_groups") or []
        selected_sender = senders[0] if len(senders) == 1 else None
        selected_group = next(
            (group for group in groups if group.get("is_default")), None
        )
        if selected_sender or selected_group:
            marketing_db.save_settings(
                user["id"],
                sender_id=(selected_sender or {}).get("id"),
                sender_name=(selected_sender or {}).get("name"),
                sender_email=(selected_sender or {}).get("email"),
                default_list_ids=[],
                suppression_group_id=(selected_group or {}).get("id"),
                suppression_group_name=(selected_group or {}).get("name"),
            )
        marketing_db.mark_test_result(user["id"])
    except IntegrationCredentialError:
        logger.exception("Email Marketing credential encryption failed")
        flash("Email Marketing credential encryption is not configured.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    except EmailCampaignProviderError as exc:
        flash(exc.user_message, "error")
        return redirect(url_for("email_marketing.settings_page"))

    flash("SendGrid connected.", "success")
    return redirect(url_for("email_marketing.settings_page"))


@email_marketing_bp.route(
    "/integrations/email-marketing/settings", methods=["POST"]
)
def save_settings():
    user, response = _auth_gate()
    if response:
        return response
    provider = _provider_for_user(user["id"])
    if not provider:
        flash("SendGrid needs to be connected.", "error")
        return redirect(url_for("email_marketing.settings_page"))

    sender_value = (request.form.get("sender_id") or "").strip()
    list_value = (request.form.get("default_list_id") or "").strip()
    group_value = (request.form.get("suppression_group_id") or "").strip()
    try:
        resources = provider.test_connection()
        senders = {str(item["id"]): item for item in resources["senders"]}
        lists = {str(item["id"]): item for item in resources["lists"]}
        groups = {
            str(item["id"]): item
            for item in resources["suppression_groups"]
        }
        if sender_value and sender_value not in senders:
            raise ValueError("Select a verified SendGrid sender.")
        if list_value and list_value not in lists:
            raise ValueError("Select a valid SendGrid Marketing list.")
        if group_value and group_value not in groups:
            raise ValueError("Select a valid SendGrid unsubscribe group.")

        sender = senders.get(sender_value)
        group = groups.get(group_value)
        marketing_db.save_settings(
            user["id"],
            sender_id=(sender or {}).get("id"),
            sender_name=(sender or {}).get("name"),
            sender_email=(sender or {}).get("email"),
            default_list_ids=[list_value] if list_value else [],
            suppression_group_id=(group or {}).get("id"),
            suppression_group_name=(group or {}).get("name"),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("email_marketing.settings_page"))
    except EmailCampaignProviderError as exc:
        flash(exc.user_message, "error")
        return redirect(url_for("email_marketing.settings_page"))

    flash("Email Marketing settings saved.", "success")
    return redirect(url_for("email_marketing.settings_page"))


@email_marketing_bp.route(
    "/integrations/email-marketing/test", methods=["POST"]
)
def test_connection():
    user, response = _auth_gate()
    if response:
        return response
    provider = _provider_for_user(user["id"])
    if not provider:
        flash("SendGrid needs to be connected.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    try:
        provider.test_connection()
        marketing_db.mark_test_result(user["id"])
        flash("SendGrid connected.", "success")
    except EmailCampaignProviderError as exc:
        marketing_db.mark_test_result(
            user["id"], error_summary=exc.user_message
        )
        flash(exc.user_message, "error")
    return redirect(url_for("email_marketing.settings_page"))


@email_marketing_bp.route(
    "/integrations/email-marketing/disconnect", methods=["POST"]
)
def disconnect():
    user, response = _auth_gate()
    if response:
        return response
    marketing_db.disconnect(user["id"])
    flash("SendGrid disconnected.", "success")
    return redirect(url_for("email_marketing.settings_page"))


@email_marketing_bp.route(
    "/listings/<int:generation_id>/email-campaigns", methods=["POST"]
)
def create_listing_campaign(generation_id):
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    if config.SUBSCRIPTION_REQUIRED and not auth.user_has_active_subscription(user):
        return jsonify({"error": "An active subscription is required."}), 403
    data = request.get_json(silent=True) or {}
    try:
        result = export_listing_email(
            user["id"],
            generation_id,
            create_another=bool(data.get("create_another")),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception:
        logger.exception(
            "Unexpected email campaign export failure user_id=%s generation_id=%s",
            user["id"],
            generation_id,
        )
        return jsonify(
            {"error": "TopAI couldn't create the SendGrid draft. Try again."}
        ), 500

    status_code = 200
    if result.get("status") in ("failed", "unknown"):
        status_code = 422
    return jsonify(result), status_code
