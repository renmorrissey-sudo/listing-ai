"""X (Twitter) — scaffolding only, no live client.

X's current API has no free posting tier: roughly $0.015/post (more if the
post contains a link) with no monthly free allotment, unlike every other
provider here. Per product decision, TopAI does not enable billed,
pay-per-post X publishing yet. This module exists so the registry/UI have a
consistent "Coming soon" entry and so a real implementation can be dropped in
later without changing calling code elsewhere.
"""

from __future__ import annotations

from social_providers.base import BaseSocialProvider, SocialProviderNotAvailable

READY = False


class XProvider(BaseSocialProvider):
    name = "x"
    display_name = "X"

    def is_app_configured(self) -> bool:
        return False

    def readiness(self) -> dict:
        return {
            "state": "coming_soon",
            "ready": False,
            "reason": (
                "X has no free posting tier (pay-per-post billing). "
                "Not enabled yet."
            ),
        }

    def get_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        raise SocialProviderNotAvailable("X posting is coming soon.", provider=self.name)

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        raise SocialProviderNotAvailable("X posting is coming soon.", provider=self.name)

    def publish_text(self, *, credentials: dict, caption: str, listing_generation: dict) -> dict:
        raise SocialProviderNotAvailable("X posting is coming soon.", provider=self.name)
