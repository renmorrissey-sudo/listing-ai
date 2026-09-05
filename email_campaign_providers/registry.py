"""Registry, readiness metadata, and factories for customer email providers."""

import config
from email_campaign_providers.constant_contact import (
    ConstantContactEmailProvider,
)
from email_campaign_providers.gmail import GmailEmailProvider
from email_campaign_providers.mailchimp import MailchimpEmailProvider
from email_campaign_providers.microsoft import MicrosoftEmailProvider
from email_campaign_providers.sendgrid import SendGridEmailCampaignProvider

PROVIDER_CLASSES = {
    "gmail": GmailEmailProvider,
    "microsoft": MicrosoftEmailProvider,
    "sendgrid": SendGridEmailCampaignProvider,
    "mailchimp": MailchimpEmailProvider,
    "constant_contact": ConstantContactEmailProvider,
}


def _oauth_config(name):
    if name == "gmail":
        return config.GOOGLE_EMAIL_CLIENT_ID, config.GOOGLE_EMAIL_CLIENT_SECRET
    if name == "microsoft":
        return (
            config.MICROSOFT_EMAIL_CLIENT_ID,
            config.MICROSOFT_EMAIL_CLIENT_SECRET,
        )
    if name == "mailchimp":
        return config.MAILCHIMP_CLIENT_ID, config.MAILCHIMP_CLIENT_SECRET
    if name == "constant_contact":
        return (
            config.CONSTANT_CONTACT_CLIENT_ID,
            config.CONSTANT_CONTACT_CLIENT_SECRET,
        )
    return "", ""


def provider_catalog():
    result = []
    for name, cls in PROVIDER_CLASSES.items():
        item = cls.capabilities.as_dict()
        item["ready"] = (
            True if name == "sendgrid" else all(_oauth_config(name))
        )
        result.append(item)
    return result


def provider_capabilities(name):
    cls = PROVIDER_CLASSES.get((name or "").strip().lower())
    return cls.capabilities if cls else None


def get_provider(
    name: str,
    *,
    api_key=None,
    access_token=None,
    provider_metadata=None,
):
    normalized = (name or "").strip().lower()
    metadata = provider_metadata or {}
    if normalized == "sendgrid":
        return SendGridEmailCampaignProvider(api_key)
    client_id, client_secret = _oauth_config(normalized)
    if not client_id or not client_secret:
        return None
    if normalized == "gmail":
        return GmailEmailProvider(
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
        )
    if normalized == "microsoft":
        return MicrosoftEmailProvider(
            client_id=client_id,
            client_secret=client_secret,
            tenant=config.MICROSOFT_EMAIL_TENANT,
            access_token=access_token,
        )
    if normalized == "mailchimp":
        return MailchimpEmailProvider(
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
            api_endpoint=metadata.get("api_endpoint"),
        )
    if normalized == "constant_contact":
        return ConstantContactEmailProvider(
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
        )
    return None
