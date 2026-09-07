"""Google Gmail OAuth and recipientless mailbox draft adapter."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from email_campaign_providers.base import (
    BaseEmailCampaignProvider,
    EmailCampaignProviderError,
    EmailProviderCapabilities,
)
from email_campaign_providers.http import query_url, request_json

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_DRAFT_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPES = (
    "openid email profile "
    "https://www.googleapis.com/auth/gmail.compose "
    "https://www.googleapis.com/auth/gmail.send"
)


class GmailEmailProvider(BaseEmailCampaignProvider):
    name = "gmail"
    display_name = "Google / Gmail"
    capabilities = EmailProviderCapabilities(
        provider=name,
        label=display_name,
        integration_kind="personal",
        auth_type="oauth",
        supports_recipientless_draft=True,
        supports_token_refresh=True,
        supports_direct_send=True,
    )

    def __init__(self, *, client_id, client_secret, access_token=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token

    def get_authorization_url(self, *, redirect_uri, state, code_challenge=None):
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
        if code_challenge:
            params.update(
                {
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                }
            )
        return query_url(AUTH_URL, params)

    def exchange_code(self, *, code, redirect_uri, code_verifier=None):
        form = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        return self._normalize_token(
            request_json("POST", TOKEN_URL, form_body=form, provider="Google")
        )

    def refresh_access_token(self, refresh_token):
        payload = request_json(
            "POST",
            TOKEN_URL,
            form_body={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            provider="Google",
        )
        return self._normalize_token(payload)

    @staticmethod
    def _normalize_token(payload):
        if not payload.get("access_token"):
            raise EmailCampaignProviderError(
                "Google did not return an access token.",
                error_code="missing_access_token",
            )
        expires = int(payload.get("expires_in") or 3600)
        payload["token_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expires)
        ).isoformat()
        return payload

    def get_identity(self):
        return request_json(
            "GET", USERINFO_URL, bearer=self.access_token, provider="Google"
        )

    def create_draft(
        self, *, subject, html_content, plain_content, sender_email=None, **_
    ):
        message = EmailMessage()
        message["Subject"] = subject
        if sender_email:
            message["From"] = sender_email
        message.set_content(plain_content)
        message.add_alternative(html_content, subtype="html")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        result = request_json(
            "POST",
            GMAIL_DRAFT_URL,
            bearer=self.access_token,
            json_body={"message": {"raw": raw}},
            provider="Google",
        )
        draft_id = result.get("id")
        if not draft_id:
            raise EmailCampaignProviderError(
                "Google did not confirm the mailbox draft.",
                error_code="missing_draft_id",
                uncertain=True,
            )
        return {
            "provider_campaign_id": str(draft_id),
            "provider_status": "draft",
            "has_recipients": False,
            "warnings": ["Choose recipients in Gmail before sending."],
        }

    def send_email(
        self,
        *,
        to_email,
        subject,
        html_content,
        plain_content,
        sender_email=None,
        attachments=None,
        **_,
    ):
        message = EmailMessage()
        message["To"] = to_email
        message["Subject"] = subject
        if sender_email:
            message["From"] = sender_email
        message.set_content(plain_content)
        message.add_alternative(html_content, subtype="html")
        for item in attachments or []:
            content_type = item.get("content_type") or "application/octet-stream"
            maintype, subtype = (
                content_type.split("/", 1)
                if "/" in content_type
                else ("application", "octet-stream")
            )
            message.add_attachment(
                item["content"],
                maintype=maintype,
                subtype=subtype,
                filename=item["filename"],
            )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        result = request_json(
            "POST",
            GMAIL_SEND_URL,
            bearer=self.access_token,
            json_body={"raw": raw},
            provider="Google",
            operation="send this email",
        )
        message_id = result.get("id")
        if not message_id:
            raise EmailCampaignProviderError(
                "Google did not confirm the email was sent.",
                error_code="missing_message_id",
                uncertain=True,
            )
        return {
            "provider_message_id": str(message_id),
            "provider_status": "sent",
        }
