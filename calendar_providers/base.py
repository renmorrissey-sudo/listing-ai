"""Shared interface + errors for calendar providers (Phase III-4).

One provider interface behind which Google lands first and Microsoft
lands second. The mock provider implements the same interface for
staging/test — selected by the resolved mock transport, never by
"is staging" and never by default (see calendar_connections_db).
"""

from __future__ import annotations


class CalendarProviderError(RuntimeError):
    """User-facing calendar error. Detailed diagnostics stay in logs."""

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


class CalendarProviderNotAvailable(CalendarProviderError):
    """Provider is gated (setup required) or not built yet."""


class BaseCalendarProvider:
    name = "base"
    display_name = "Base Provider"

    def readiness(self) -> dict:
        """Non-secret status for the Calendar Connections UI and confirm gating.

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
        external_account_id, display_name, scopes, calendar_id}.
        Raises CalendarProviderError on failure.
        """
        raise NotImplementedError

    def refresh_access_token(self, *, refresh_token: str) -> dict:
        """Exchange a refresh token for a fresh access token.

        Returns {access_token, expires_at (iso, optional)}.
        Raises CalendarProviderError on failure (revoked/expired refresh
        token surfaces as needs_reconnect=True).
        """
        raise NotImplementedError

    def list_events(self, *, credentials: dict, start_utc: str, end_utc: str) -> list:
        """Events overlapping [start_utc, end_utc).

        Each event is {id, summary, start_at (UTC ISO), end_at (UTC ISO)}.
        Raises CalendarProviderError on failure.
        """
        raise NotImplementedError

    def create_event(self, *, credentials: dict, event: dict) -> dict:
        """Create an event. ``event`` carries summary, start_at/end_at
        (UTC ISO), description (optional). Returns {id, ...}.
        Raises CalendarProviderError on failure.
        """
        raise NotImplementedError

    def update_event(self, *, credentials: dict, event_id: str, event: dict) -> dict:
        """Move/replace an event. Returns the updated event dict.
        Raises CalendarProviderError on failure.
        """
        raise NotImplementedError

    def delete_event(self, *, credentials: dict, event_id: str) -> None:
        """Delete an event. Raises CalendarProviderError on failure."""
        raise NotImplementedError
