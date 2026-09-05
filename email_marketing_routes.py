"""Tenant-owned email integration settings, OAuth, and listing draft export."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timezone

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import auth
import config
import email_marketing_db as marketing_db
from email_campaign_providers.base import EmailCampaignProviderError
from email_campaign_providers.registry import (
    get_provider,
    provider_capabilities,
    provider_catalog,
)
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


def _callback_url(provider):
    return (
        f"{config.APP_BASE_URL}"
        f"{url_for('email_marketing.oauth_callback', provider=provider)}"
    )


def _provider_for_integration(user_id, integration_id):
    try:
        connection = marketing_db.get_integration_credentials(
            user_id, integration_id
        )
    except IntegrationCredentialError:
        logger.exception(
            "Could not decrypt customer email credentials user_id=%s integration_id=%s",
            user_id,
            integration_id,
        )
        return None, None
    if not connection:
        return None, None
    provider = get_provider(
        connection["provider"],
        api_key=connection.get("credential"),
        access_token=connection.get("access_token"),
        provider_metadata=connection.get("provider_metadata"),
    )
    return connection, provider


def _provider_for_user(user_id):
    """Compatibility helper for the original single-SendGrid settings routes."""
    integration_id = _legacy_sendgrid_id(user_id)
    if not integration_id:
        return None
    _, provider = _provider_for_integration(user_id, integration_id)
    return provider


def _load_resources(user_id):
    provider = _provider_for_user(user_id)
    if not provider:
        return {"senders": [], "lists": [], "suppression_groups": []}, None
    try:
        return _resource_bundle(provider), None
    except EmailCampaignProviderError as exc:
        return {"senders": [], "lists": [], "suppression_groups": []}, exc.user_message


def _resource_bundle(provider):
    if not provider:
        return {"senders": [], "lists": [], "suppression_groups": []}
    result = provider.test_connection()
    if not isinstance(result, dict):
        result = {}
    if "senders" not in result:
        result["senders"] = provider.get_senders()
    if "lists" not in result:
        result["lists"] = provider.get_lists()
    if "suppression_groups" not in result:
        result["suppression_groups"] = provider.get_suppression_groups()
    return result


@email_marketing_bp.route("/integrations/email-marketing", methods=["GET"])
def settings_page():
    user, response = _auth_gate()
    if response:
        return response
    integrations = marketing_db.list_integrations(user["id"])
    selected_id = request.args.get("integration_id", type=int)
    selected = next(
        (item for item in integrations if item["id"] == selected_id), None
    )
    resources = {"senders": [], "lists": [], "suppression_groups": []}
    resource_error = None
    if selected and selected.get("status") == "connected":
        _, provider = _provider_for_integration(user["id"], selected["id"])
        try:
            resources = _resource_bundle(provider)
        except EmailCampaignProviderError as exc:
            resource_error = exc.user_message
    return render_template(
        "email_marketing_settings.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="email-marketing",
        product_name=config.PRODUCT_NAME,
        integrations=integrations,
        selected=selected,
        providers=provider_catalog(),
        senders=resources.get("senders") or [],
        lists=resources.get("lists") or [],
        suppression_groups=resources.get("suppression_groups") or [],
        resource_error=resource_error,
        encryption_ready=is_configured(),
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/connect", methods=["POST"]
)
@email_marketing_bp.route(
    "/integrations/email-marketing/sendgrid/connect", methods=["POST"]
)
def connect_sendgrid():
    user, response = _auth_gate()
    if response:
        return response
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        flash("Enter a SendGrid Marketing API key.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    if not is_configured():
        flash("Email credential encryption is not configured.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    try:
        provider = get_provider("sendgrid", api_key=api_key)
        identity = (
            provider.get_identity()
            if hasattr(provider, "get_identity")
            else {}
        )
        resources = provider.test_connection()
        senders = resources.get("senders") or []
        groups = resources.get("suppression_groups") or []
        sender = senders[0] if len(senders) == 1 else None
        group = next((row for row in groups if row.get("is_default")), None)
        # Keep the rollout compatibility row current while all product behavior
        # uses the generic integration record below.
        marketing_db.connect(
            user["id"], api_key, mirror_generic=False
        )
        marketing_db.save_settings(
            user["id"],
            sender_id=(sender or {}).get("id"),
            sender_name=(sender or {}).get("name"),
            sender_email=(sender or {}).get("email"),
            default_list_ids=[],
            suppression_group_id=(group or {}).get("id"),
            suppression_group_name=(group or {}).get("name"),
            mirror_generic=False,
        )
        external_id = (
            identity.get("id")
            or identity.get("email")
            or hashlib.sha256(api_key.encode()).hexdigest()[:24]
        )
        connection = marketing_db.upsert_integration(
            user["id"],
            "sendgrid",
            integration_kind="marketing",
            auth_type="api_key",
            external_account_id=external_id,
            external_account_email=identity.get("email"),
            display_name=identity.get("name") or "SendGrid",
            credential=api_key,
            sender_id=(sender or {}).get("id"),
            sender_name=(sender or {}).get("name"),
            sender_email=(sender or {}).get("email"),
            settings={
                "default_list_ids": [],
                "suppression_group_id": (group or {}).get("id"),
                "suppression_group_name": (group or {}).get("name"),
            },
        )
        marketing_db.mark_integration_test_result(
            user["id"], connection["id"]
        )
    except (EmailCampaignProviderError, IntegrationCredentialError) as exc:
        flash(
            getattr(exc, "user_message", None)
            or "Email credential encryption is not configured.",
            "error",
        )
        return redirect(url_for("email_marketing.settings_page"))
    flash("SendGrid Marketing connected.", "success")
    return redirect(
        url_for(
            "email_marketing.settings_page", integration_id=connection["id"]
        )
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/oauth/<provider>/start", methods=["GET"]
)
def oauth_start(provider):
    user, response = _auth_gate()
    if response:
        return response
    capabilities = provider_capabilities(provider)
    oauth_provider = get_provider(provider)
    if (
        not capabilities
        or capabilities.auth_type != "oauth"
        or not oauth_provider
    ):
        flash("This email provider requires administrator OAuth setup.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    if not is_configured():
        flash("Email credential encryption is not configured.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    redirect_uri = _callback_url(provider)
    state = marketing_db.create_oauth_state(
        user["id"],
        provider,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )
    return redirect(
        oauth_provider.get_authorization_url(
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
        )
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/oauth/<provider>/callback", methods=["GET"]
)
def oauth_callback(provider):
    user, response = _auth_gate()
    if response:
        return response
    if request.args.get("error"):
        flash("The email provider connection was cancelled or denied.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    state = marketing_db.consume_oauth_state(
        request.args.get("state"), provider, user["id"]
    )
    if not state:
        flash("This email connection link is invalid or expired.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    code = request.args.get("code")
    oauth_provider = get_provider(provider)
    if not code or not oauth_provider:
        flash("The email provider did not return a valid authorization.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    try:
        tokens = oauth_provider.exchange_code(
            code=code,
            redirect_uri=state["redirect_uri"],
            code_verifier=state.get("code_verifier"),
        )
        oauth_provider.access_token = tokens["access_token"]
        identity = oauth_provider.get_identity()
        metadata = {}
        if provider == "mailchimp":
            metadata["api_endpoint"] = identity.get("api_endpoint")
            external_id = identity.get("user_id") or identity.get("login", {}).get(
                "login_id"
            )
            account_email = identity.get("login", {}).get("email")
            display_name = identity.get("accountname") or "Mailchimp"
        elif provider == "microsoft":
            external_id = identity.get("id")
            account_email = identity.get("mail") or identity.get(
                "userPrincipalName"
            )
            display_name = identity.get("displayName") or account_email
        elif provider == "constant_contact":
            external_id = identity.get("account_id")
            account_email = identity.get("email_address")
            display_name = (
                identity.get("organization_name") or account_email
            )
        else:
            external_id = identity.get("sub") or identity.get("id")
            account_email = identity.get("email")
            display_name = identity.get("name") or account_email
        if not external_id:
            raise EmailCampaignProviderError(
                "The provider did not identify the connected account.",
                error_code="missing_account_identity",
            )
        capabilities = provider_capabilities(provider)
        connection = marketing_db.upsert_integration(
            user["id"],
            provider,
            integration_kind=capabilities.integration_kind,
            auth_type="oauth",
            external_account_id=external_id,
            external_account_email=account_email,
            display_name=display_name,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            token_expires_at=tokens.get("token_expires_at"),
            scopes=tokens.get("scope"),
            sender_name=display_name,
            sender_email=account_email,
            provider_metadata=metadata,
        )
        marketing_db.mark_integration_test_result(
            user["id"], connection["id"]
        )
    except (EmailCampaignProviderError, IntegrationCredentialError) as exc:
        flash(
            getattr(exc, "user_message", None)
            or "TopAI could not securely save this email account.",
            "error",
        )
        return redirect(url_for("email_marketing.settings_page"))
    flash(f"{capabilities.label} connected.", "success")
    return redirect(
        url_for(
            "email_marketing.settings_page", integration_id=connection["id"]
        )
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/connections/<int:integration_id>/settings",
    methods=["POST"],
)
def save_settings(integration_id):
    user, response = _auth_gate()
    if response:
        return response
    connection, provider = _provider_for_integration(user["id"], integration_id)
    if not connection or not provider:
        flash("Email account not found.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    try:
        resources = _resource_bundle(provider)
        senders = {str(row["id"]): row for row in resources["senders"]}
        lists = {str(row["id"]): row for row in resources["lists"]}
        groups = {
            str(row["id"]): row for row in resources["suppression_groups"]
        }
        sender_value = (request.form.get("sender_id") or "").strip()
        list_values = [
            value.strip()
            for value in request.form.getlist("default_list_id")
            if value.strip()
        ]
        group_value = (request.form.get("suppression_group_id") or "").strip()
        if sender_value and sender_value not in senders:
            raise ValueError("Select a sender returned by this provider.")
        if any(value not in lists for value in list_values):
            raise ValueError("Select a valid list for this account.")
        if group_value and group_value not in groups:
            raise ValueError("Select a valid unsubscribe group.")
        sender = senders.get(sender_value)
        if not sender and connection["provider"] == "mailchimp":
            sender_name = (request.form.get("sender_name") or "").strip()
            sender_email = (request.form.get("sender_email") or "").strip()
        else:
            sender_name = (sender or {}).get("name")
            sender_email = (sender or {}).get("email")
        group = groups.get(group_value)
        marketing_db.save_integration_settings(
            user["id"],
            integration_id,
            sender_id=(sender or {}).get("id"),
            sender_name=sender_name,
            sender_email=sender_email,
            settings={
                "default_list_ids": list_values,
                "suppression_group_id": (group or {}).get("id"),
                "suppression_group_name": (group or {}).get("name"),
            },
        )
    except (ValueError, EmailCampaignProviderError) as exc:
        flash(getattr(exc, "user_message", str(exc)), "error")
    else:
        flash("Email account settings saved.", "success")
    return redirect(
        url_for(
            "email_marketing.settings_page", integration_id=integration_id
        )
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/connections/<int:integration_id>/test",
    methods=["POST"],
)
def test_connection(integration_id):
    user, response = _auth_gate()
    if response:
        return response
    connection, provider = _provider_for_integration(user["id"], integration_id)
    if not connection or not provider:
        flash("Email account not found or provider setup is incomplete.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    try:
        _resource_bundle(provider)
        marketing_db.mark_integration_test_result(user["id"], integration_id)
        flash(f"{connection['display_name']} is connected.", "success")
    except EmailCampaignProviderError as exc:
        marketing_db.mark_integration_test_result(
            user["id"], integration_id, error_summary=exc.user_message
        )
        if exc.reconnect_required:
            marketing_db.mark_integration_needs_reconnect(
                user["id"], integration_id, exc.user_message
            )
        flash(exc.user_message, "error")
    return redirect(
        url_for(
            "email_marketing.settings_page", integration_id=integration_id
        )
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/connections/<int:integration_id>/default",
    methods=["POST"],
)
def set_default(integration_id):
    user, response = _auth_gate()
    if response:
        return response
    if marketing_db.set_default_integration(user["id"], integration_id):
        flash("Default email account updated.", "success")
    else:
        flash("Email account not found.", "error")
    return redirect(url_for("email_marketing.settings_page"))


@email_marketing_bp.route(
    "/integrations/email-marketing/connections/<int:integration_id>/disconnect",
    methods=["POST"],
)
def disconnect(integration_id):
    user, response = _auth_gate()
    if response:
        return response
    if marketing_db.disconnect_integration(user["id"], integration_id):
        flash("Email account disconnected and stored credentials removed.", "success")
    else:
        flash("Email account not found.", "error")
    return redirect(url_for("email_marketing.settings_page"))


def _legacy_sendgrid_id(user_id):
    default = marketing_db.get_default_integration(user_id)
    if default and default.get("provider") == "sendgrid":
        return default["id"]
    return next(
        (
            item["id"]
            for item in marketing_db.list_integrations(user_id)
            if item.get("provider") == "sendgrid"
            and item.get("status") == "connected"
        ),
        None,
    )


@email_marketing_bp.route(
    "/integrations/email-marketing/settings", methods=["POST"]
)
def legacy_save_settings():
    user, response = _auth_gate()
    if response:
        return response
    provider = _provider_for_user(user["id"])
    if not provider:
        flash("SendGrid Marketing needs to be connected.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    sender_value = (request.form.get("sender_id") or "").strip()
    list_value = (request.form.get("default_list_id") or "").strip()
    group_value = (request.form.get("suppression_group_id") or "").strip()
    try:
        resources = provider.test_connection()
        senders = {str(row["id"]): row for row in resources["senders"]}
        lists = {str(row["id"]): row for row in resources["lists"]}
        groups = {
            str(row["id"]): row for row in resources["suppression_groups"]
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
        flash("Email Marketing settings saved.", "success")
    except (ValueError, EmailCampaignProviderError) as exc:
        flash(getattr(exc, "user_message", str(exc)), "error")
    return redirect(url_for("email_marketing.settings_page"))


@email_marketing_bp.route(
    "/integrations/email-marketing/test", methods=["POST"]
)
def legacy_test_connection():
    user, response = _auth_gate()
    if response:
        return response
    integration_id = _legacy_sendgrid_id(user["id"])
    if not integration_id:
        flash("SendGrid Marketing needs to be connected.", "error")
        return redirect(url_for("email_marketing.settings_page"))
    return test_connection(integration_id)


@email_marketing_bp.route(
    "/integrations/email-marketing/disconnect", methods=["POST"]
)
def legacy_disconnect():
    user, response = _auth_gate()
    if response:
        return response
    integration_id = _legacy_sendgrid_id(user["id"])
    if not integration_id:
        return redirect(url_for("email_marketing.settings_page"))
    return disconnect(integration_id)


@email_marketing_bp.route(
    "/integrations/email-marketing/options", methods=["GET"]
)
def integration_options():
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    options = marketing_db.list_integrations(user["id"], connected_only=True)
    return jsonify({"items": options})


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
            integration_id=data.get("integration_id"),
            create_another=bool(data.get("create_another")),
        )
    except ValueError as exc:
        status = 404 if "listing" in str(exc).lower() else 422
        return jsonify({"error": str(exc)}), status
    except Exception:
        logger.exception(
            "Unexpected email draft export failure user_id=%s generation_id=%s",
            user["id"],
            generation_id,
        )
        return jsonify({"error": "TopAI couldn't create the email draft."}), 500
    return jsonify(result), (
        422 if result.get("status") in ("failed", "unknown") else 200
    )
