"""Twilio SendGrid New Marketing Campaigns / Single Sends provider."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from email_campaign_providers.base import (
    BaseEmailCampaignProvider,
    EmailCampaignProviderError,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.sendgrid.com/v3"


class SendGridEmailCampaignProvider(BaseEmailCampaignProvider):
    name = "sendgrid"
    display_name = "SendGrid"

    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise EmailCampaignProviderError(
                "SendGrid needs to be connected.",
                error_code="not_connected",
            )

    def _request(self, method: str, path: str, *, body=None):
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{API_BASE}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "TopAI-Real-Estate-Tools/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # Keep provider details server-side and API keys out of logs.
            raw = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "SendGrid Marketing API failed method=%s path=%s status=%s body=%s",
                method,
                path,
                exc.code,
                raw[:500],
            )
            if exc.code in (401, 403):
                raise EmailCampaignProviderError(
                    "SendGrid needs to be connected with an API key that has "
                    "Marketing Campaigns access.",
                    error_code="authentication_failed",
                ) from None
            if exc.code == 400:
                raise EmailCampaignProviderError(
                    "SendGrid couldn't create this draft. Check the selected "
                    "sender, list, and unsubscribe group.",
                    error_code="invalid_configuration",
                ) from None
            raise EmailCampaignProviderError(
                "TopAI couldn't create the SendGrid draft. Try again.",
                error_code=f"http_{exc.code}",
                uncertain=exc.code >= 500,
            ) from None
        except urllib.error.URLError:
            logger.warning(
                "SendGrid Marketing API network failure method=%s path=%s",
                method,
                path,
            )
            raise EmailCampaignProviderError(
                "TopAI couldn't confirm whether SendGrid created the draft. "
                "Check SendGrid before trying again.",
                error_code="network_error",
                uncertain=True,
            ) from None
        except (ValueError, json.JSONDecodeError):
            raise EmailCampaignProviderError(
                "SendGrid returned an unexpected response. Try again.",
                error_code="invalid_response",
                uncertain=method != "GET",
            ) from None

    def get_senders(self):
        payload = self._request("GET", "/verified_senders")
        if isinstance(payload, list):
            rows = payload
        else:
            rows = payload.get("results") or payload.get("senders") or []
        result = []
        for row in rows:
            if not row.get("verified"):
                continue
            result.append(
                {
                    "id": int(row["id"]),
                    "name": row.get("nickname")
                    or row.get("from_name")
                    or row.get("name")
                    or row.get("from_email"),
                    "email": row.get("from_email") or row.get("email"),
                }
            )
        return result

    def get_lists(self):
        payload = self._request("GET", "/marketing/lists?page_size=1000")
        rows = payload.get("result") or []
        return [
            {
                "id": str(row["id"]),
                "name": row.get("name") or str(row["id"]),
                "contact_count": int(row.get("contact_count") or 0),
            }
            for row in rows
            if row.get("id")
        ]

    def get_suppression_groups(self):
        payload = self._request("GET", "/asm/groups")
        rows = payload if isinstance(payload, list) else payload.get("result") or []
        return [
            {
                "id": int(row["id"]),
                "name": row.get("name") or str(row["id"]),
                "description": row.get("description"),
                "is_default": bool(row.get("is_default")),
            }
            for row in rows
            if row.get("id") is not None
        ]

    def test_connection(self):
        """Read-only validation; never creates, schedules, or sends anything."""
        senders = self.get_senders()
        lists = self.get_lists()
        groups = self.get_suppression_groups()
        return {
            "connected": True,
            "senders": senders,
            "lists": lists,
            "suppression_groups": groups,
        }

    def create_draft(
        self,
        *,
        name: str,
        subject: str,
        html_content: str,
        plain_content: str,
        sender_id=None,
        list_ids=None,
        suppression_group_id=None,
    ):
        """Create an unscheduled Single Send draft.

        Deliberately calls only POST /marketing/singlesends. There is no
        schedule/send method in this provider abstraction.
        """
        email_config = {
            "subject": subject,
            "html_content": html_content,
            "plain_content": plain_content,
            "generate_plain_content": False,
            "editor": "code",
        }
        if sender_id not in (None, ""):
            email_config["sender_id"] = int(sender_id)
        if suppression_group_id not in (None, ""):
            email_config["suppression_group_id"] = int(
                suppression_group_id
            )

        payload = {
            "name": (name or "TopAI Listing Campaign")[:100],
            "categories": ["TopAI", "Listing Generator"],
            "email_config": email_config,
        }
        selected_lists = [str(item) for item in (list_ids or []) if item]
        if selected_lists:
            payload["send_to"] = {"list_ids": selected_lists}
        # No list: omit send_to. Never set all=true.

        result = self._request("POST", "/marketing/singlesends", body=payload)
        campaign_id = result.get("id")
        if not campaign_id:
            raise EmailCampaignProviderError(
                "SendGrid didn't confirm the campaign draft. Check SendGrid "
                "before trying again.",
                error_code="missing_campaign_id",
                uncertain=True,
            )
        return {
            "provider_campaign_id": str(campaign_id),
            "provider_status": result.get("status") or "draft",
            "warnings": result.get("warnings") or [],
            "has_recipients": bool(selected_lists),
        }
