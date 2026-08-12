"""Provider registry for email marketing campaign integrations."""

from email_campaign_providers.sendgrid import SendGridEmailCampaignProvider


def get_provider(name: str, *, api_key: str):
    normalized = (name or "").strip().lower()
    if normalized == "sendgrid":
        return SendGridEmailCampaignProvider(api_key)
    return None
