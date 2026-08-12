"""Export saved Listing Generator email snapshots as provider campaign drafts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import email_marketing_db as marketing_db
import listing_generations_db as listing_db
from email_campaign_providers.base import EmailCampaignProviderError
from email_campaign_providers.registry import get_provider
from listing_email_content import (
    parse_listing_email,
    render_listing_email_html,
)
from integration_credentials import IntegrationCredentialError

logger = logging.getLogger(__name__)


def _campaign_name(address: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{address} - {stamp}"[:100]


def export_listing_email(
    user_id,
    listing_generation_id,
    *,
    create_another=False,
):
    generation = listing_db.get_by_id(user_id, listing_generation_id)
    if not generation:
        raise ValueError("Listing not found or no longer retained.")
    snapshot = generation.get("output_snapshot") or {}
    parsed = parse_listing_email(
        snapshot.get("email"), generation.get("display_address")
    )

    export, created = marketing_db.create_or_get_export(
        user_id,
        generation["id"],
        property_address=generation["display_address"],
        subject=parsed["subject"],
        create_another=bool(create_another),
    )
    if not created:
        result = marketing_db.public_export(export)
        result["already_exists"] = True
        result["has_recipients"] = None
        return result

    try:
        credentials = marketing_db.get_credentials(user_id)
    except IntegrationCredentialError:
        logger.exception(
            "Email Marketing credential decryption failed user_id=%s", user_id
        )
        credentials = None
    if not credentials:
        row = marketing_db.update_export(
            user_id,
            export["id"],
            status="failed",
            error_code="not_connected",
            error_summary="SendGrid needs to be connected.",
        )
        return marketing_db.public_export(row)
    if not credentials.get("sender_id"):
        row = marketing_db.update_export(
            user_id,
            export["id"],
            status="failed",
            error_code="sender_required",
            error_summary=(
                "No verified email sender is configured. Choose one in "
                "Email Marketing settings."
            ),
        )
        return marketing_db.public_export(row)
    if not credentials.get("suppression_group_id"):
        row = marketing_db.update_export(
            user_id,
            export["id"],
            status="failed",
            error_code="suppression_group_required",
            error_summary=(
                "Select an unsubscribe group in Email Marketing settings."
            ),
        )
        return marketing_db.public_export(row)

    provider = get_provider("sendgrid", api_key=credentials["api_key"])
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
            sender_id=credentials.get("sender_id"),
            list_ids=credentials.get("default_list_ids"),
            suppression_group_id=credentials.get("suppression_group_id"),
        )
    except EmailCampaignProviderError as exc:
        logger.warning(
            "Email campaign draft failed user_id=%s generation_id=%s "
            "provider=sendgrid code=%s uncertain=%s",
            user_id,
            listing_generation_id,
            exc.error_code,
            exc.uncertain,
        )
        row = marketing_db.update_export(
            user_id,
            export["id"],
            status="unknown" if exc.uncertain else "failed",
            error_code=exc.error_code,
            error_summary=exc.user_message,
        )
        return marketing_db.public_export(row)

    row = marketing_db.update_export(
        user_id,
        export["id"],
        status="draft_created",
        provider_campaign_id=provider_result["provider_campaign_id"],
        provider_status=provider_result.get("provider_status") or "draft",
    )
    result = marketing_db.public_export(row)
    result["already_exists"] = False
    result["has_recipients"] = provider_result.get("has_recipients")
    return result
