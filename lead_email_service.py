"""One-to-one CRM lead email sending through tenant-owned integrations."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import crm_db
import db
import email_marketing_db as marketing_db
from email_campaign_providers.base import EmailCampaignProviderError
from email_campaign_providers.registry import get_provider, provider_capabilities
from integration_credentials import IntegrationCredentialError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _valid_email(value):
    return bool(EMAIL_RE.match(str(value or "").strip()))


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


def _choose_send_integration(user_id, integration_id=None):
    candidates = []
    if integration_id:
        selected = marketing_db.get_integration(user_id, integration_id)
        if selected:
            candidates.append(selected)
    else:
        default = marketing_db.get_default_integration(user_id)
        if default:
            candidates.append(default)
        for item in marketing_db.list_integrations(user_id, connected_only=True):
            if not any(existing["id"] == item["id"] for existing in candidates):
                candidates.append(item)
    for item in candidates:
        capabilities = provider_capabilities(item.get("provider"))
        if capabilities and capabilities.supports_direct_send:
            return item
    return None


def _render_html(body):
    paragraphs = [
        f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>"
        for part in re.split(r"\n\s*\n", str(body or "").strip())
        if part.strip()
    ]
    return "".join(paragraphs) or "<p></p>"


def _first_name(value):
    return str(value or "").strip().split(" ")[0] if str(value or "").strip() else "there"


def _greeting_line(body):
    lines = str(body or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _has_greeting(body):
    first_line = _greeting_line(body).lower()
    return first_line.startswith(("hi ", "hello ", "dear ", "hey "))


def _personalize_greeting(lead, body):
    first = _first_name(lead.get("name"))
    desired = f"Hi {first},"
    stripped = str(body or "").strip()
    if not stripped:
        return stripped
    first_line = _greeting_line(stripped)
    generic = first_line.lower().rstrip(",.!") in {"hi", "hello", "hey", "hi there", "hello there", "hey there", "dear lead"}
    if generic:
        return stripped.replace(first_line, desired, 1)
    if not _has_greeting(stripped):
        return f"{desired}\n\n{stripped}"
    return stripped


def _email_signature(user_id):
    profile = db.get_business_profile(user_id) or {}
    lines = ["Warm regards,"]
    for value in (
        profile.get("agent_name"),
        profile.get("phone_number"),
        profile.get("brokerage_name") or profile.get("company_name"),
    ):
        text = str(value or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def personalize_lead_email_body(user_id, lead, body):
    body = str(body or "").strip()
    if not body:
        return body
    body = _personalize_greeting(lead, body)
    signature = _email_signature(user_id)
    if signature and "warm regards" not in body.lower():
        body = f"{body.rstrip()}\n\n{signature}"
    return body[:5000]


def send_direct_email(
    user_id,
    *,
    to_email,
    subject,
    body,
    attachments=None,
    integration_id=None,
):
    """Send a direct email through the user's connected provider."""
    to_email = str(to_email or "").strip()
    subject = str(subject or "").strip()[:200]
    body = str(body or "").strip()[:10000]
    if not _valid_email(to_email):
        return None, "Enter a valid recipient email address."
    if not subject or not body:
        return None, "Subject and message are required before sending email."

    normalized_attachments = []
    total_size = 0
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        filename = str(item.get("filename") or "").strip()[:180]
        content_type = str(item.get("content_type") or "application/octet-stream").strip()[:120]
        if not filename or not isinstance(content, bytes):
            return None, "TopAI could not prepare the email attachment."
        total_size += len(content)
        normalized_attachments.append(
            {
                "filename": filename,
                "content_type": content_type,
                "content": content,
            }
        )
    if total_size > 10 * 1024 * 1024:
        return None, "The email attachment is too large to send."

    integration = _choose_send_integration(user_id, integration_id=integration_id)
    if not integration:
        return None, "Connect Gmail, Microsoft 365, or SendGrid before sending email."
    try:
        connection = marketing_db.get_integration_credentials(user_id, integration["id"])
    except IntegrationCredentialError:
        return None, "TopAI could not securely read this email connection. Reconnect it."
    if not connection:
        return None, "This email account needs to be reconnected."

    try:
        connection = _refresh_if_needed(user_id, connection)
        provider = _provider(connection)
        if not provider:
            return None, "This email provider is not ready for sending."
        result = provider.send_email(
            to_email=to_email,
            subject=subject,
            html_content=_render_html(body),
            plain_content=body,
            attachments=normalized_attachments,
            sender_id=connection.get("sender_id"),
            sender_name=connection.get("sender_name") or connection.get("display_name"),
            sender_email=connection.get("sender_email")
            or connection.get("external_account_email"),
        )
    except (ValueError, EmailCampaignProviderError) as exc:
        message = exc.user_message if isinstance(exc, EmailCampaignProviderError) else str(exc)
        return None, message

    return {
        "ok": True,
        "to_email": to_email,
        "subject": subject,
        "provider": integration.get("provider"),
        "integration_id": integration.get("id"),
        "provider_message_id": result.get("provider_message_id"),
        "provider_status": result.get("provider_status") or "sent",
        "sent_at": _now(),
    }, None


def send_lead_email(
    user_id,
    lead_id,
    *,
    subject,
    body,
    integration_id=None,
    actor_user_id=None,
):
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    to_email = str(lead.get("email") or "").strip()
    if not _valid_email(to_email):
        return None, "This lead does not have a valid email address."
    subject = str(subject or "").strip()[:200]
    body = str(body or "").strip()[:5000]
    if not subject or not body:
        return None, "Subject and body are required before sending email."
    body = personalize_lead_email_body(user_id, lead, body)

    integration = _choose_send_integration(user_id, integration_id=integration_id)
    if not integration:
        return (
            None,
            "Connect Gmail, Microsoft 365, or SendGrid before sending lead emails.",
        )
    try:
        connection = marketing_db.get_integration_credentials(user_id, integration["id"])
    except IntegrationCredentialError:
        return None, "TopAI could not securely read this email connection. Reconnect it."
    if not connection:
        return None, "This email account needs to be reconnected."
    try:
        connection = _refresh_if_needed(user_id, connection)
        provider = _provider(connection)
        if not provider:
            return None, "This email provider is not ready for sending."
        result = provider.send_email(
            to_email=to_email,
            subject=subject,
            html_content=_render_html(body),
            plain_content=body,
            sender_id=connection.get("sender_id"),
            sender_name=connection.get("sender_name")
            or connection.get("display_name"),
            sender_email=connection.get("sender_email")
            or connection.get("external_account_email"),
        )
    except (ValueError, EmailCampaignProviderError) as exc:
        message = exc.user_message if isinstance(exc, EmailCampaignProviderError) else str(exc)
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "email_send_failed",
            f"Email send failed: {subject}",
            {
                "subject": subject,
                "to_email": to_email,
                "provider": integration.get("provider"),
                "error": message[:500],
            },
            actor_user_id=actor_user_id or user_id,
        )
        return None, message

    payload = {
        "subject": subject,
        "to_email": to_email,
        "provider": integration.get("provider"),
        "integration_id": integration.get("id"),
        "provider_message_id": result.get("provider_message_id"),
        "provider_status": result.get("provider_status") or "sent",
        "sent_at": _now(),
    }
    activity_id = crm_db.add_lead_activity(
        lead_id,
        user_id,
        "email_sent",
        f"Email sent to {to_email}: {subject}",
        payload,
        actor_user_id=actor_user_id or user_id,
    )
    return {
        "ok": True,
        "activity_id": activity_id,
        "lead_id": lead_id,
        "lead_name": lead.get("name") or "Lead",
        **payload,
    }, None
