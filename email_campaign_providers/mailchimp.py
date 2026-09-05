"""Mailchimp OAuth and unsent regular-campaign draft adapter."""

from __future__ import annotations

from email_campaign_providers.base import (
    BaseEmailCampaignProvider,
    EmailCampaignProviderError,
    EmailProviderCapabilities,
)
from email_campaign_providers.http import query_url, request_json

AUTH_URL = "https://login.mailchimp.com/oauth2/authorize"
TOKEN_URL = "https://login.mailchimp.com/oauth2/token"
METADATA_URL = "https://login.mailchimp.com/oauth2/metadata"


class MailchimpEmailProvider(BaseEmailCampaignProvider):
    name = "mailchimp"
    display_name = "Mailchimp"
    capabilities = EmailProviderCapabilities(
        provider=name,
        label=display_name,
        integration_kind="marketing",
        auth_type="oauth",
        requires_sender=True,
        requires_list=True,
        supports_lists=True,
    )

    def __init__(
        self,
        *,
        client_id,
        client_secret,
        access_token=None,
        api_endpoint=None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.api_endpoint = (api_endpoint or "").rstrip("/")

    def get_authorization_url(self, *, redirect_uri, state, code_challenge=None):
        return query_url(
            AUTH_URL,
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
            },
        )

    def exchange_code(self, *, code, redirect_uri, code_verifier=None):
        return request_json(
            "POST",
            TOKEN_URL,
            form_body={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            provider="Mailchimp",
        )

    def get_identity(self):
        metadata = request_json(
            "GET", METADATA_URL, bearer=self.access_token, provider="Mailchimp"
        )
        if metadata.get("api_endpoint"):
            self.api_endpoint = metadata["api_endpoint"].rstrip("/")
        return metadata

    def _api(self, method, path, *, body=None):
        if not self.api_endpoint:
            raise EmailCampaignProviderError(
                "Reconnect Mailchimp so TopAI can identify its data center.",
                error_code="missing_api_endpoint",
                reconnect_required=True,
            )
        return request_json(
            method,
            f"{self.api_endpoint}/3.0{path}",
            bearer=self.access_token,
            json_body=body,
            provider="Mailchimp",
        )

    def get_lists(self):
        payload = self._api(
            "GET",
            "/lists?count=1000&fields=lists.id,lists.name,lists.stats.member_count",
        )
        return [
            {
                "id": row["id"],
                "name": row.get("name") or row["id"],
                "contact_count": (row.get("stats") or {}).get("member_count", 0),
            }
            for row in payload.get("lists") or []
            if row.get("id")
        ]

    def create_draft(
        self,
        *,
        name,
        subject,
        html_content,
        sender_name,
        sender_email,
        list_ids=None,
        **_,
    ):
        selected = [str(item) for item in (list_ids or []) if item]
        if not selected:
            raise EmailCampaignProviderError(
                "Choose a Mailchimp audience before creating the draft.",
                error_code="list_required",
            )
        if not sender_name or not sender_email:
            raise EmailCampaignProviderError(
                "Configure a sender name and reply-to email for Mailchimp.",
                error_code="sender_required",
            )
        campaign = self._api(
            "POST",
            "/campaigns",
            body={
                "type": "regular",
                "recipients": {"list_id": selected[0]},
                "settings": {
                    "subject_line": subject,
                    "title": (name or "TopAI Listing Campaign")[:100],
                    "from_name": sender_name,
                    "reply_to": sender_email,
                },
            },
        )
        campaign_id = campaign.get("id")
        if not campaign_id:
            raise EmailCampaignProviderError(
                "Mailchimp did not confirm the campaign draft.",
                error_code="missing_campaign_id",
                uncertain=True,
            )
        self._api(
            "PUT",
            f"/campaigns/{campaign_id}/content",
            body={"html": html_content},
        )
        return {
            "provider_campaign_id": str(campaign_id),
            "provider_status": campaign.get("status") or "save",
            "has_recipients": True,
            "warnings": [],
        }
