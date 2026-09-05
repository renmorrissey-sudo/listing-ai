"""Microsoft 365 OAuth and recipientless Outlook draft adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from email_campaign_providers.base import (
    BaseEmailCampaignProvider,
    EmailCampaignProviderError,
    EmailProviderCapabilities,
)
from email_campaign_providers.http import query_url, request_json

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = "openid profile email offline_access User.Read Mail.ReadWrite Mail.Send"


class MicrosoftEmailProvider(BaseEmailCampaignProvider):
    name = "microsoft"
    display_name = "Microsoft 365 / Outlook"
    capabilities = EmailProviderCapabilities(
        provider=name,
        label=display_name,
        integration_kind="personal",
        auth_type="oauth",
        supports_recipientless_draft=True,
        supports_token_refresh=True,
        supports_direct_send=True,
    )

    def __init__(
        self, *, client_id, client_secret, tenant="common", access_token=None
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant = tenant or "common"
        self.access_token = access_token

    @property
    def authorize_url(self):
        return (
            f"https://login.microsoftonline.com/{self.tenant}"
            "/oauth2/v2.0/authorize"
        )

    @property
    def token_url(self):
        return (
            f"https://login.microsoftonline.com/{self.tenant}"
            "/oauth2/v2.0/token"
        )

    def get_authorization_url(self, *, redirect_uri, state, code_challenge=None):
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": SCOPES,
            "state": state,
        }
        if code_challenge:
            params.update(
                {
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                }
            )
        return query_url(self.authorize_url, params)

    def exchange_code(self, *, code, redirect_uri, code_verifier=None):
        form = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        return self._normalize_token(
            request_json(
                "POST", self.token_url, form_body=form, provider="Microsoft"
            )
        )

    def refresh_access_token(self, refresh_token):
        return self._normalize_token(
            request_json(
                "POST",
                self.token_url,
                form_body={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": SCOPES,
                },
                provider="Microsoft",
            )
        )

    @staticmethod
    def _normalize_token(payload):
        if not payload.get("access_token"):
            raise EmailCampaignProviderError(
                "Microsoft did not return an access token.",
                error_code="missing_access_token",
            )
        expires = int(payload.get("expires_in") or 3600)
        payload["token_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expires)
        ).isoformat()
        return payload

    def get_identity(self):
        return request_json(
            "GET",
            f"{GRAPH_BASE}/me?$select=id,displayName,mail,userPrincipalName",
            bearer=self.access_token,
            provider="Microsoft",
        )

    def create_draft(self, *, subject, html_content, **_):
        result = request_json(
            "POST",
            f"{GRAPH_BASE}/me/messages",
            bearer=self.access_token,
            json_body={
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_content},
                "toRecipients": [],
            },
            provider="Microsoft",
        )
        draft_id = result.get("id")
        if not draft_id:
            raise EmailCampaignProviderError(
                "Microsoft did not confirm the Outlook draft.",
                error_code="missing_draft_id",
                uncertain=True,
            )
        return {
            "provider_campaign_id": str(draft_id),
            "provider_status": "draft",
            "has_recipients": False,
            "warnings": ["Choose recipients in Outlook before sending."],
        }

    def send_email(
        self,
        *,
        to_email,
        subject,
        html_content,
        plain_content=None,
        **_,
    ):
        request_json(
            "POST",
            f"{GRAPH_BASE}/me/sendMail",
            bearer=self.access_token,
            json_body={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html_content},
                    "toRecipients": [
                        {"emailAddress": {"address": to_email}},
                    ],
                },
                "saveToSentItems": True,
            },
            provider="Microsoft",
            operation="send this email",
        )
        return {
            "provider_message_id": None,
            "provider_status": "sent",
        }
