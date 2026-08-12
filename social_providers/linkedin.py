"""LinkedIn OAuth (member profile) + Posts API publishing.

Live and self-serve — no Meta/TikTok-style app review is required for posting
to a member's own profile with `w_member_social` once the LinkedIn app has the
"Sign In with LinkedIn using OpenID Connect" and "Share on LinkedIn" products
added (a same-day, self-service step in the LinkedIn Developer Portal — see
final report for the exact console steps).

Uses the current LinkedIn Posts API (`/rest/posts`), not the deprecated
UGC Posts API. Self-serve access tokens are long-lived (~60 days) but do NOT
include a refresh token, so an expired connection surfaces as "needs
reconnect" rather than being silently refreshed.
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

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
LINKEDIN_API_VERSION = "202401"
SCOPES = "openid profile w_member_social"


class LinkedInProvider(BaseSocialProvider):
    name = "linkedin"
    display_name = "LinkedIn"

    def is_app_configured(self) -> bool:
        return bool((config.LINKEDIN_CLIENT_ID or "").strip() and (config.LINKEDIN_CLIENT_SECRET or "").strip())

    def readiness(self) -> dict:
        if self.is_app_configured():
            return {"state": "live", "ready": True, "reason": None}
        return {
            "state": "setup_required",
            "ready": False,
            "reason": "LinkedIn app credentials are not configured yet.",
        }

    def get_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "response_type": "code",
            "client_id": config.LINKEDIN_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": SCOPES,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _post_form(self, url, form: dict):
        data = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error("LinkedIn token exchange failed http=%s detail=%s", exc.code, detail[:300])
            raise SocialProviderError(
                "LinkedIn couldn't complete the connection. Please try again.",
                provider=self.name,
            ) from None
        except urllib.error.URLError:
            raise SocialProviderError(
                "Couldn't reach LinkedIn. Please try again.", retryable=True, provider=self.name
            ) from None

    def _get_json(self, url, access_token):
        req = urllib.request.Request(
            url, method="GET", headers={"Authorization": f"Bearer {access_token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error("LinkedIn userinfo failed http=%s detail=%s", exc.code, detail[:300])
            raise SocialProviderError(
                "LinkedIn couldn't verify your account. Please reconnect.",
                needs_reconnect=exc.code in (401, 403),
                provider=self.name,
            ) from None
        except urllib.error.URLError:
            raise SocialProviderError(
                "Couldn't reach LinkedIn. Please try again.", retryable=True, provider=self.name
            ) from None

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        token_resp = self._post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": config.LINKEDIN_CLIENT_ID,
                "client_secret": config.LINKEDIN_CLIENT_SECRET,
            },
        )
        access_token = token_resp.get("access_token")
        if not access_token:
            raise SocialProviderError("LinkedIn didn't return an access token.", provider=self.name)
        expires_in = token_resp.get("expires_in")
        expires_at = None
        if expires_in:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()

        identity = self._get_json(USERINFO_URL, access_token)
        member_id = identity.get("sub")
        if not member_id:
            raise SocialProviderError("LinkedIn didn't return a member id.", provider=self.name)
        return {
            "access_token": access_token,
            "refresh_token": token_resp.get("refresh_token"),
            "expires_at": expires_at,
            "external_account_id": f"urn:li:person:{member_id}",
            "display_name": identity.get("name") or identity.get("given_name") or "LinkedIn member",
            "scopes": SCOPES,
        }

    def publish_text(self, *, credentials: dict, caption: str, listing_generation: dict) -> dict:
        access_token = credentials.get("access_token")
        author_urn = credentials.get("external_account_id")
        if not access_token or not author_urn:
            raise SocialProviderError(
                "Your LinkedIn connection has expired. Reconnect LinkedIn.",
                needs_reconnect=True,
                provider=self.name,
            )
        body = {
            "author": author_urn,
            "commentary": (caption or "").strip()[:3000],
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        req = urllib.request.Request(
            POSTS_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
                "LinkedIn-Version": LINKEDIN_API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                post_urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error("LinkedIn publish failed http=%s detail=%s", exc.code, detail[:300])
            if exc.code in (401, 403):
                raise SocialProviderError(
                    "Your LinkedIn connection has expired. Reconnect LinkedIn.",
                    needs_reconnect=True,
                    provider=self.name,
                ) from None
            raise SocialProviderError(
                "LinkedIn couldn't publish this post. Try again.",
                retryable=exc.code >= 500 or exc.code == 429,
                provider=self.name,
            ) from None
        except urllib.error.URLError:
            raise SocialProviderError(
                "Couldn't reach LinkedIn. Try again.", retryable=True, provider=self.name
            ) from None

        if not post_urn:
            raise SocialProviderError("LinkedIn didn't confirm the post. Try again.", provider=self.name)
        return {
            "provider_post_id": post_urn,
            "provider_post_url": f"https://www.linkedin.com/feed/update/{urllib.parse.quote(post_urn)}/",
        }
