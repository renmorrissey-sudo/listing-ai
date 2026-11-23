"""Single lookup point for calendar provider instances + readiness.

Google lands first; Microsoft is a second module on the same interface
and is NOT built in this phase — ``get_provider("microsoft")`` returns
None (an unknown provider is rejected, never silently defaulted).
The mock provider is listed here for resolution but is never a
user-facing connect option; see calendar_connections_db for the
mock-transport selection rule.
"""

from __future__ import annotations

from calendar_providers.google import GoogleProvider
from calendar_providers.mock import MockCalendarProvider

PROVIDER_ORDER = ["google"]

_INSTANCES = {
    "google": GoogleProvider(),
    "mock": MockCalendarProvider(),
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
