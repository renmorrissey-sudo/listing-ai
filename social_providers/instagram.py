"""Instagram professional-account OAuth (via Facebook Login) + Content Publishing API.

Gated the same way as Facebook (Meta App Review + Business Verification for
`instagram_content_publish`/`instagram_basic`). Additionally — independent of
review status — the Instagram Content Publishing API structurally requires an
image or video URL for every post (`/{ig-id}/media` with `image_url`/
`video_url`, then `/{ig-id}/media_publish`). The Listing Generator only
produces text content today, so `publish_text` always reports this as
unavailable until image generation exists, even once Meta approval lands.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

import config
from social_providers.base import BaseSocialProvider, SocialProviderError, SocialProviderNotAvailable

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
AUTHORIZE_URL = f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"
SCOPES = "instagram_basic,instagram_content_publish,pages_read_engagement,pages_show_list"


class InstagramProvider(BaseSocialProvider):
    name = "instagram"
    display_name = "Instagram"

    def is_app_configured(self) -> bool:
        # Shares the same Meta app as Facebook.
        return bool((config.FACEBOOK_APP_ID or "").strip() and (config.FACEBOOK_APP_SECRET or "").strip())

    def readiness(self) -> dict:
        if not self.is_app_configured():
            return {
                "state": "setup_required",
                "ready": False,
                "reason": "Facebook/Instagram app credentials are not configured yet.",
            }
        if not config.META_APP_REVIEW_APPROVED:
            return {
                "state": "setup_required",
                "ready": False,
                "reason": (
                    "Waiting on Meta App Review + Business Verification for "
                    "instagram_content_publish before Instagram can be connected."
                ),
            }
        return {
            "state": "live",
            "ready": True,
            "reason": None,
            "publish_note": (
                "Instagram requires a photo or video with every post — the "
                "Listing Generator doesn't produce images yet, so posts here "
                "will report 'requires a photo' until that's added."
            ),
        }

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
            logger.error("Instagram API call failed http=%s detail=%s", exc.code, detail[:300])
            raise SocialProviderError(
                "Instagram couldn't complete that request. Please try again.",
                needs_reconnect=exc.code in (401, 403),
                provider=self.name,
            ) from None
        except urllib.error.URLError:
            raise SocialProviderError(
                "Couldn't reach Instagram. Try again.", retryable=True, provider=self.name
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
            raise SocialProviderError("Instagram didn't return an access token.", provider=self.name)

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

        pages = self._get_json(
            f"{GRAPH_BASE}/me/accounts?" + urllib.parse.urlencode({"access_token": user_token})
        ).get("data") or []
        ig_account_id = None
        page_name = None
        for page in pages:
            details = self._get_json(
                f"{GRAPH_BASE}/{page['id']}?"
                + urllib.parse.urlencode(
                    {"fields": "instagram_business_account", "access_token": user_token}
                )
            )
            ig = details.get("instagram_business_account")
            if ig and ig.get("id"):
                ig_account_id = ig["id"]
                page_name = page.get("name")
                break
        if not ig_account_id:
            raise SocialProviderError(
                "No Instagram professional account is linked to your Facebook Pages.",
                provider=self.name,
            )
        return {
            "access_token": user_token,
            "refresh_token": None,
            "expires_at": None,
            "external_account_id": ig_account_id,
            "display_name": page_name or "Instagram account",
            "scopes": SCOPES,
        }

    def publish_text(self, *, credentials: dict, caption: str, listing_generation: dict) -> dict:
        raise SocialProviderNotAvailable(
            "Instagram requires a photo with every post. This listing has no "
            "generated image yet, so it can't be posted to Instagram.",
            provider=self.name,
        )
