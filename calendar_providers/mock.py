"""Mock calendar provider (Phase III-4).

In-memory implementation of the provider interface for staging/test.
Selected by the resolved mock transport (see calendar_connections_db),
never by "is staging" and never by default — it must never run in
production. Process-local: all confirm executions for one tenant run
in the harness worker process, so the mock's in-memory store is
consistent for a live verification run. Tests use :func:`clear` /
:func:`seed_event` to control state.
"""

from __future__ import annotations

import uuid

from calendar_providers.base import BaseCalendarProvider

# {user_id: {event_id: event dict}}
_STORE: dict = {}


def _events_for(user_id):
    return _STORE.setdefault(user_id, {})


def clear(user_id=None):
    """Reset mock state (tests + staging setup)."""
    if user_id is None:
        _STORE.clear()
    else:
        _STORE.pop(user_id, None)


def seed_event(user_id, event: dict) -> dict:
    """Insert an event directly (tests: a colliding external event)."""
    event_id = str(event.get("id") or f"mock-{uuid.uuid4().hex[:12]}")
    stored = {
        "id": event_id,
        "summary": str(event.get("summary") or "Mock event"),
        "start_at": event["start_at"],
        "end_at": event["end_at"],
    }
    _events_for(user_id)[event_id] = stored
    return dict(stored)


def list_events_for(user_id):
    return [dict(e) for e in _events_for(user_id).values()]


class MockCalendarProvider(BaseCalendarProvider):
    name = "mock"
    display_name = "Mock Calendar"

    def readiness(self) -> dict:
        return {"state": "live", "ready": True, "reason": None}

    def is_app_configured(self) -> bool:
        return True

    def get_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        raise NotImplementedError("The mock provider has no OAuth flow.")

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        raise NotImplementedError("The mock provider has no OAuth flow.")

    def refresh_access_token(self, *, refresh_token: str) -> dict:
        return {"access_token": "mock-access-token", "expires_at": None}

    def list_events(self, *, credentials: dict, start_utc: str, end_utc: str) -> list:
        user_id = (credentials or {}).get("user_id")
        events = []
        for event in _events_for(user_id).values():
            if event["start_at"] < end_utc and event["end_at"] > start_utc:
                events.append(dict(event))
        events.sort(key=lambda e: e["start_at"])
        return events

    def create_event(self, *, credentials: dict, event: dict) -> dict:
        user_id = (credentials or {}).get("user_id")
        return seed_event(
            user_id,
            {
                "summary": event.get("summary") or "Appointment",
                "start_at": event["start_at"],
                "end_at": event["end_at"],
            },
        )

    def update_event(self, *, credentials: dict, event_id: str, event: dict) -> dict:
        user_id = (credentials or {}).get("user_id")
        stored = _events_for(user_id).get(event_id)
        if stored is None:
            return self.create_event(credentials=credentials, event=event)
        stored.update(
            {
                "summary": str(event.get("summary") or stored.get("summary") or "Appointment"),
                "start_at": event["start_at"],
                "end_at": event["end_at"],
            }
        )
        return dict(stored)

    def delete_event(self, *, credentials: dict, event_id: str) -> None:
        user_id = (credentials or {}).get("user_id")
        _events_for(user_id).pop(event_id, None)
