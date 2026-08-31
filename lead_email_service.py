"""One-to-one CRM lead email sending through tenant-owned SendGrid."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import crm_db
import db
import email_marketing_db
from email_campaign_providers.base import EmailCampaignProviderError
from email_campaign_providers.sendgrid import SendGridEmailCampaignProvider
from integration_credentials import IntegrationCredentialError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _valid_email(value):
    return bool(EMAIL_RE.match(str(value or "").strip()))


def _render_html(body):
    paragraphs = [
        f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>"
        for part in re.split(r"\n\s*\n", str(body or "").strip())
        if part.strip()
    ]
    return "".join(paragraphs) or "<p></p>"


def send_lead_email(user_id, lead_id, *, subject, body, actor_user_id=None):
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

    try:
        credentials = email_marketing_db.get_credentials(user_id, "sendgrid")
    except IntegrationCredentialError:
        return None, "TopAI could not securely read this SendGrid connection. Reconnect it."
    if not credentials:
        return None, "Connect SendGrid before sending lead emails."

    try:
        provider = SendGridEmailCampaignProvider(credentials["api_key"])
        result = provider.send_email(
            to_email=to_email,
            subject=subject,
            html_content=_render_html(body),
            plain_content=body,
            sender_name=credentials.get("sender_name"),
            sender_email=credentials.get("sender_email"),
        )
    except EmailCampaignProviderError as exc:
        crm_db.add_lead_activity(
            lead_id,
            user_id,
            "email_send_failed",
            f"Email send failed: {subject}",
            {
                "subject": subject,
                "to_email": to_email,
                "provider": "sendgrid",
                "error": exc.user_message[:500],
            },
            actor_user_id=actor_user_id or user_id,
        )
        return None, exc.user_message

    payload = {
        "subject": subject,
        "to_email": to_email,
        "provider": "sendgrid",
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
