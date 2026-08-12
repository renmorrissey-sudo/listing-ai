"""TikTok OAuth + Content Posting API (Direct Post).

Gated behind `config.TIKTOK_AUDIT_APPROVED`: unaudited TikTok apps are capped
at 5 total authorized users per 24 hours and forced to `SELF_ONLY` post
visibility, so this cannot be offered broadly until TikTok's app audit is
submitted and approved.

Independent of audit status, TikTok's Content Posting API publishes photo or
video content, not plain text — the Listing Generator has no video/photo
output today, so `publish_text` always reports this as unavailable.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
from social_providers.base import BaseSocialProvider, SocialProviderError, SocialProviderNotAvailable

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = "user.info.basic,video.publish"


class TikTokProvider(BaseSocialProvider):
    name = "tiktok"
    display_name = "TikTok"

    def is_app_configured(self) -> bool:
        return bool((config.TIKTOK_CLIENT_KEY or "").strip() and (config.TIKTOK_CLIENT_SECRET or "").strip())

    def readiness(self) -> dict:
        if not self.is_app_configured():
            return {
                "state": "setup_required",
                "ready": False,
                "reason": "TikTok app credentials are not configured yet.",
            }
        if not config.TIKTOK_AUDIT_APPROVED:
            return {
                "state": "setup_required",
                "ready": False,
                "reason": (
                    "Waiting on TikTok's app audit before accounts beyond a "
                    "handful of testers can be connected."
                ),
            }
        return {
            "state": "live",
            "ready": True,
            "reason": None,
            "publish_note": (
                "TikTok requires a photo or video with every post — the "
                "Listing Generator doesn't produce media yet, so posts here "
                "will report 'requires a video' until that's added."
            ),
        }

    def get_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_key": config.TIKTOK_CLIENT_KEY,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        data = urllib.parse.urlencode(
            {
                "client_key": config.TIKTOK_CLIENT_KEY,
                "client_secret": config.TIKTOK_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                token_resp = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error("TikTok token exchange failed http=%s detail=%s", exc.code, detail[:300])
            raise SocialProviderError(
                "TikTok couldn't complete the connection. Please try again.",
                provider=self.name,
            ) from None
        except urllib.error.URLError:
            raise SocialProviderError(
                "Couldn't reach TikTok. Try again.", retryable=True, provider=self.name
            ) from None

        access_token = token_resp.get("access_token")
        open_id = token_resp.get("open_id")
        if not access_token or not open_id:
            raise SocialProviderError("TikTok didn't return an access token.", provider=self.name)
        expires_in = token_resp.get("expires_in")
        expires_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
            if expires_in
            else None
        )
        return {
            "access_token": access_token,
            "refresh_token": token_resp.get("refresh_token"),
            "expires_at": expires_at,
            "external_account_id": open_id,
            "display_name": "TikTok account",
            "scopes": token_resp.get("scope") or SCOPES,
        }

    def publish_text(self, *, credentials: dict, caption: str, listing_generation: dict) -> dict:
        raise SocialProviderNotAvailable(
            "TikTok requires a video with every post. This listing has no "
            "generated video yet, so it can't be posted to TikTok.",
            provider=self.name,
        )
