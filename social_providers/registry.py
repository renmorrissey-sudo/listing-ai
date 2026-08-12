"""Single lookup point for provider instances + readiness, used by both the
Social Media Connections UI and the one-click Post publish orchestrator."""

from __future__ import annotations

from social_providers.facebook import FacebookProvider
from social_providers.instagram import InstagramProvider
from social_providers.linkedin import LinkedInProvider
from social_providers.tiktok import TikTokProvider
from social_providers.x import XProvider

PROVIDER_ORDER = ["linkedin", "facebook", "instagram", "tiktok", "x"]

_INSTANCES = {
    "linkedin": LinkedInProvider(),
    "facebook": FacebookProvider(),
    "instagram": InstagramProvider(),
    "tiktok": TikTokProvider(),
    "x": XProvider(),
}


def get_provider(name: str):
    return _INSTANCES.get((name or "").strip().lower())


def all_providers():
    return [_INSTANCES[name] for name in PROVIDER_ORDER]


def readiness_for(name: str) -> dict:
    provider = get_provider(name)
    if not provider:
        return {"state": "unknown", "ready": False, "reason": "Unknown provider."}
    return provider.readiness()


def is_ready(name: str) -> bool:
    return bool(readiness_for(name).get("ready"))
