"""Facebook Page OAuth (Facebook Login for Business) + Pages API publishing.

Gated behind `config.META_APP_REVIEW_APPROVED`: Meta requires App Review +
Business Verification for `pages_manage_posts`/`pages_show_list` before any
Page not owned by an app admin/tester can be connected or posted to. Until
that review is approved, `readiness()` reports "setup_required" and the
Connect button stays disabled — this module is never called with a live
user in that state, per social_routes.py's readiness gate.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
from social_providers.base import BaseSocialProvider, SocialProviderError

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
AUTHORIZE_URL = f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"
SCOPES = "pages_show_list,pages_manage_posts,pages_read_engagement,pages_manage_metadata"


class FacebookProvider(BaseSocialProvider):
    name = "facebook"
    display_name = "Facebook"

    def is_app_configured(self) -> bool:
        return bool((config.FACEBOOK_APP_ID or "").strip() and (config.FACEBOOK_APP_SECRET or "").strip())

    def readiness(self) -> dict:
        if not self.is_app_configured():
            return {
                "state": "setup_required",
                "ready": False,
                "reason": "Facebook app credentials are not configured yet.",
            }
        if not config.META_APP_REVIEW_APPROVED:
            return {
                "state": "setup_required",
                "ready": False,
                "reason": (
                    "Waiting on Meta App Review + Business Verification for "
                    "pages_manage_posts before Facebook Pages can be connected."
                ),
            }
        return {"state": "live", "ready": True, "reason": None}

    def get_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": config.FACEBOOK_APP_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": SCOPES,
            "response_type": "code",
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _get_json(self, url):
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error("Facebook API call failed http=%s detail=%s", exc.code, detail[:300])
            raise SocialProviderError(
                "Facebook couldn't complete that request. Please try again.",
                needs_reconnect=exc.code in (401, 403),
                provider=self.name,
            ) from None
        except urllib.error.URLError:
            raise SocialProviderError(
                "Couldn't reach Facebook. Try again.", retryable=True, provider=self.name
            ) from None

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        short_lived = self._get_json(
            f"{GRAPH_BASE}/oauth/access_token?"
            + urllib.parse.urlencode(
                {
                    "client_id": config.FACEBOOK_APP_ID,
                    "redirect_uri": redirect_uri,
                    "client_secret": config.FACEBOOK_APP_SECRET,
                    "code": code,
                }
            )
        )
        user_token = short_lived.get("access_token")
        if not user_token:
            raise SocialProviderError("Facebook didn't return an access token.", provider=self.name)

        long_lived = self._get_json(
            f"{GRAPH_BASE}/oauth/access_token?"
            + urllib.parse.urlencode(
                {
                    "grant_type": "fb_exchange_token",
                    "client_id": config.FACEBOOK_APP_ID,
                    "client_secret": config.FACEBOOK_APP_SECRET,
                    "fb_exchange_token": user_token,
                }
            )
        )
        user_token = long_lived.get("access_token") or user_token
        expires_in = long_lived.get("expires_in")
        expires_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
            if expires_in
            else None
        )

        accounts = self._get_json(
            f"{GRAPH_BASE}/me/accounts?" + urllib.parse.urlencode({"access_token": user_token})
        )
        pages = accounts.get("data") or []
        if not pages:
            raise SocialProviderError(
                "No Facebook Page is available to connect. You must be an admin of a Page.",
                provider=self.name,
            )
        page = pages[0]
        return {
            "access_token": page.get("access_token") or user_token,
            "refresh_token": None,
            "expires_at": expires_at,
            "external_account_id": page.get("id"),
            "display_name": page.get("name") or "Facebook Page",
            "scopes": SCOPES,
        }

    def publish_text(self, *, credentials: dict, caption: str, listing_generation: dict) -> dict:
        access_token = credentials.get("access_token")
        page_id = credentials.get("external_account_id")
        if not access_token or not page_id:
            raise SocialProviderError(
                "Your Facebook Page connection needs attention. Reconnect Facebook.",
                needs_reconnect=True,
                provider=self.name,
            )
        data = urllib.parse.urlencode(
            {"message": (caption or "").strip()[:63000], "access_token": access_token}
        ).encode("utf-8")
        req = urllib.request.Request(f"{GRAPH_BASE}/{page_id}/feed", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error("Facebook publish failed http=%s detail=%s", exc.code, detail[:300])
            if exc.code in (401, 403):
                raise SocialProviderError(
                    "Your Facebook Page connection needs attention. Reconnect Facebook.",
                    needs_reconnect=True,
                    provider=self.name,
                ) from None
            raise SocialProviderError(
                "Facebook couldn't publish this post. Try again.",
                retryable=exc.code >= 500 or exc.code == 429,
                provider=self.name,
            ) from None
        except urllib.error.URLError:
            raise SocialProviderError(
                "Couldn't reach Facebook. Try again.", retryable=True, provider=self.name
            ) from None

        post_id = result.get("id")
        if not post_id:
            raise SocialProviderError("Facebook didn't confirm the post. Try again.", provider=self.name)
        return {
            "provider_post_id": post_id,
            "provider_post_url": f"https://www.facebook.com/{post_id}",
        }
