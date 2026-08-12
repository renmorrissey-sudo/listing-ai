"""Shared interface + errors for social media OAuth/publishing providers."""

from __future__ import annotations


class SocialProviderError(RuntimeError):
    """User-facing publishing/connection error. Detailed diagnostics stay in logs."""

    def __init__(
        self,
        user_message: str,
        *,
        retryable: bool = False,
        needs_reconnect: bool = False,
        error_code: str | None = None,
        provider: str | None = None,
    ):
        super().__init__(user_message)
        self.user_message = user_message
        self.retryable = retryable
        self.needs_reconnect = needs_reconnect
        self.error_code = error_code
        self.provider = provider


class SocialProviderNotAvailable(SocialProviderError):
    """Provider is gated (setup required / coming soon) or cannot post this content
    type yet (e.g. Instagram/TikTok require an image/video the app doesn't generate)."""


class BaseSocialProvider:
    name = "base"
    display_name = "Base Provider"

    def readiness(self) -> dict:
        """Non-secret status for the Social Media Connections UI and publish gating.

        Returns {"state": "live"|"setup_required"|"coming_soon", "ready": bool, "reason": str|None}
        """
        raise NotImplementedError

    def is_app_configured(self) -> bool:
        """Whether TopAI's own OAuth client id/secret are present for this provider."""
        raise NotImplementedError

    def get_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        raise NotImplementedError

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        """Exchange an OAuth code for tokens + identity.

        Returns {access_token, refresh_token (optional), expires_at (iso, optional),
        external_account_id, display_name, scopes}.
        Raises SocialProviderError on failure.
        """
        raise NotImplementedError

    def publish_text(self, *, credentials: dict, caption: str, listing_generation: dict) -> dict:
        """Publish a text post. Returns {provider_post_id, provider_post_url}.

        Raises SocialProviderError (SocialProviderNotAvailable if structurally
        unsupported, e.g. Instagram/TikTok without an image).
        """
        raise NotImplementedError
