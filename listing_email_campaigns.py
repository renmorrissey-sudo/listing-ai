"""Export saved Listing Generator email snapshots through a tenant integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import email_marketing_db as marketing_db
import listing_generations_db as listing_db
from email_campaign_providers.base import EmailCampaignProviderError
from email_campaign_providers.registry import get_provider, provider_capabilities
from integration_credentials import IntegrationCredentialError
from listing_email_content import parse_listing_email, render_listing_email_html

logger = logging.getLogger(__name__)


def _campaign_name(address):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{address} - {stamp}"[:100]


def _token_needs_refresh(value):
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return expires_at <= datetime.now(timezone.utc) + timedelta(minutes=5)


def _provider(connection):
    return get_provider(
        connection["provider"],
        api_key=connection.get("credential"),
        access_token=connection.get("access_token"),
        provider_metadata=connection.get("provider_metadata"),
    )


def _refresh_if_needed(user_id, connection):
    capabilities = provider_capabilities(connection["provider"])
    if (
        not capabilities
        or not capabilities.supports_token_refresh
        or not _token_needs_refresh(connection.get("token_expires_at"))
    ):
        return connection
    if not connection.get("refresh_token"):
        marketing_db.mark_integration_needs_reconnect(
            user_id,
            connection["id"],
            "Authorization expired. Reconnect this email account.",
        )
        raise ValueError("Authorization expired. Reconnect this email account.")
    provider = _provider(connection)
    try:
        tokens = provider.refresh_access_token(connection["refresh_token"])
        marketing_db.update_integration_tokens(
            user_id,
            connection["id"],
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            token_expires_at=tokens.get("token_expires_at"),
            scopes=tokens.get("scope"),
        )
    except EmailCampaignProviderError as exc:
        if exc.reconnect_required or exc.error_code == "authentication_failed":
            marketing_db.mark_integration_needs_reconnect(
                user_id, connection["id"], exc.user_message
            )
        raise ValueError(exc.user_message) from None
    return marketing_db.get_integration_credentials(user_id, connection["id"])


def _validate(connection):
    capabilities = provider_capabilities(connection["provider"])
    if not capabilities:
        raise ValueError("This email provider is not supported.")
    settings = connection.get("settings") or {}
    if capabilities.requires_sender and not connection.get("sender_email"):
        raise ValueError(
            "Configure a sender identity in Email Marketing settings."
        )
    if capabilities.requires_list and not settings.get("default_list_ids"):
        raise ValueError(
            "Choose a recipient audience in Email Marketing settings."
        )
    if (
        connection["provider"] == "sendgrid"
        and not settings.get("suppression_group_id")
    ):
        raise ValueError(
            "Select an unsubscribe group in Email Marketing settings."
        )


def export_listing_email(
    user_id,
    listing_generation_id,
    *,
    integration_id=None,
    create_another=False,
):
    generation = listing_db.get_by_id(user_id, listing_generation_id)
    if not generation:
        raise ValueError("Listing not found or no longer retained.")
    integration = (
        marketing_db.get_integration(user_id, integration_id)
        if integration_id
        else marketing_db.get_default_integration(user_id)
    )
    if not integration or integration.get("status") != "connected":
        raise ValueError(
            "Connect an email account and choose a default before creating a draft."
        )
    try:
        connection = marketing_db.get_integration_credentials(
            user_id, integration["id"]
        )
    except IntegrationCredentialError:
        logger.exception(
            "Customer email credential decryption failed user_id=%s integration_id=%s",
            user_id,
            integration["id"],
        )
        raise ValueError(
            "TopAI could not securely read this connection. Reconnect it."
        ) from None
    if not connection:
        raise ValueError("This email account needs to be reconnected.")
    connection = _refresh_if_needed(user_id, connection)
    _validate(connection)
    provider = _provider(connection)
    if not provider:
        raise ValueError(
            "This provider requires administrator OAuth setup before it can be used."
        )

    snapshot = generation.get("output_snapshot") or {}
    parsed = parse_listing_email(
        snapshot.get("email"), generation.get("display_address")
    )
    export, created = marketing_db.create_or_get_integration_export(
        user_id,
        generation["id"],
        integration,
        property_address=generation["display_address"],
        subject=parsed["subject"],
        create_another=bool(create_another),
    )
    if not created:
        result = marketing_db.public_integration_export(export)
        result["already_exists"] = True
        result["has_recipients"] = None
        return result

    settings = connection.get("settings") or {}
    html_content = render_listing_email_html(
        subject=parsed["subject"],
        body=parsed["body"],
        property_address=generation["display_address"],
    )
    try:
        provider_result = provider.create_draft(
            name=_campaign_name(generation["display_address"]),
            subject=parsed["subject"],
            html_content=html_content,
            plain_content=parsed["body"],
            sender_id=(
                int(connection["sender_id"])
                if connection["provider"] == "sendgrid"
                and connection.get("sender_id")
                else connection.get("sender_id")
            ),
            sender_name=connection.get("sender_name"),
            sender_email=connection.get("sender_email")
            or connection.get("external_account_email"),
            list_ids=settings.get("default_list_ids") or [],
            suppression_group_id=settings.get("suppression_group_id"),
        )
    except EmailCampaignProviderError as exc:
        logger.warning(
            "Customer email draft failed user_id=%s generation_id=%s "
            "integration_id=%s provider=%s code=%s uncertain=%s",
            user_id,
            listing_generation_id,
            integration["id"],
            integration["provider"],
            exc.error_code,
            exc.uncertain,
        )
        if exc.reconnect_required:
            marketing_db.mark_integration_needs_reconnect(
                user_id, integration["id"], exc.user_message
            )
        row = marketing_db.update_export(
            user_id,
            export["id"],
            status="unknown" if exc.uncertain else "failed",
            error_code=exc.error_code,
            error_summary=exc.user_message,
        )
        return marketing_db.public_integration_export(row)

    row = marketing_db.update_export(
        user_id,
        export["id"],
        status="draft_created",
        provider_campaign_id=provider_result["provider_campaign_id"],
        provider_status=provider_result.get("provider_status") or "draft",
    )
    result = marketing_db.public_integration_export(row)
    result.update(
        {
            "already_exists": False,
            "has_recipients": provider_result.get("has_recipients"),
            "warnings": provider_result.get("warnings") or [],
        }
    )
    return result
