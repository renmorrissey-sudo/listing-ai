"""Constant Contact V3 OAuth and unsent campaign-draft adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from email_campaign_providers.base import (
    BaseEmailCampaignProvider,
    EmailCampaignProviderError,
    EmailProviderCapabilities,
)
from email_campaign_providers.http import query_url, request_json

AUTH_URL = "https://authz.constantcontact.com/oauth2/default/v1/authorize"
TOKEN_URL = "https://authz.constantcontact.com/oauth2/default/v1/token"
API_BASE = "https://api.cc.email/v3"
SCOPES = "account_read contact_data campaign_data offline_access"


class ConstantContactEmailProvider(BaseEmailCampaignProvider):
    name = "constant_contact"
    display_name = "Constant Contact"
    capabilities = EmailProviderCapabilities(
        provider=name,
        label=display_name,
        integration_kind="marketing",
        auth_type="oauth",
        requires_sender=True,
        supports_lists=True,
        supports_token_refresh=True,
    )

    def __init__(
        self, *, client_id, client_secret, access_token=None
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token

    def get_authorization_url(self, *, redirect_uri, state, code_challenge=None):
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
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
        return query_url(AUTH_URL, params)

    def exchange_code(self, *, code, redirect_uri, code_verifier=None):
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        return self._normalize_token(
            request_json(
                "POST",
                TOKEN_URL,
                basic=(self.client_id, self.client_secret),
                form_body=form,
                provider="Constant Contact",
            )
        )

    def refresh_access_token(self, refresh_token):
        return self._normalize_token(
            request_json(
                "POST",
                TOKEN_URL,
                basic=(self.client_id, self.client_secret),
                form_body={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                provider="Constant Contact",
            )
        )

    @staticmethod
    def _normalize_token(payload):
        if not payload.get("access_token"):
            raise EmailCampaignProviderError(
                "Constant Contact did not return an access token.",
                error_code="missing_access_token",
            )
        expires = int(payload.get("expires_in") or 7200)
        payload["token_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expires)
        ).isoformat()
        return payload

    def _api(self, method, path, *, body=None):
        return request_json(
            method,
            f"{API_BASE}{path}",
            bearer=self.access_token,
            json_body=body,
            provider="Constant Contact",
        )

    def get_identity(self):
        return self._api("GET", "/account/summary")

    def get_senders(self):
        payload = self._api("GET", "/account/emails?status=CONFIRMED")
        rows = payload if isinstance(payload, list) else payload.get("_embedded", {}).get("emails", [])
        return [
            {
                "id": row.get("email_id") or row.get("email_address"),
                "name": row.get("email_address"),
                "email": row.get("email_address"),
            }
            for row in rows
            if row.get("email_address")
        ]

    def get_lists(self):
        payload = self._api("GET", "/contact_lists?status=ACTIVE&limit=500")
        rows = payload.get("_embedded", {}).get("contact_lists", [])
        return [
            {
                "id": row["list_id"],
                "name": row.get("name") or row["list_id"],
                "contact_count": row.get("membership_count", 0),
            }
            for row in rows
            if row.get("list_id")
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
        if not sender_name or not sender_email:
            raise EmailCampaignProviderError(
                "Choose a confirmed Constant Contact sender.",
                error_code="sender_required",
            )
        activity = {
            "role": "PRIMARY_EMAIL",
            "format_type": 5,
            "from_email": sender_email,
            "from_name": sender_name,
            "reply_to_email": sender_email,
            "subject": subject,
            "html_content": html_content,
        }
        selected = [str(item) for item in (list_ids or []) if item]
        if selected:
            activity["contact_list_ids"] = selected
        result = self._api(
            "POST",
            "/emails",
            body={
                "name": (name or "TopAI Listing Campaign")[:80],
                "email_campaign_activities": [activity],
            },
        )
        campaign_id = result.get("campaign_id")
        if not campaign_id:
            raise EmailCampaignProviderError(
                "Constant Contact did not confirm the campaign draft.",
                error_code="missing_campaign_id",
                uncertain=True,
            )
        return {
            "provider_campaign_id": str(campaign_id),
            "provider_status": result.get("current_status") or "DRAFT",
            "has_recipients": bool(selected),
            "warnings": (
                [] if selected else ["Choose recipients in Constant Contact."]
            ),
        }
