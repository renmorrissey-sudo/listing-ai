"""Phase III-4 calendar provider tests.

Provider interface + mock + Google OAuth-shape unit tests. The mock
never resolves in production config (mock transport off); unknown
providers are rejected; Google's authorize URL and token exchange are
shape-tested against a stubbed HTTP layer (no network, no real OAuth
client — none exists yet; the tracked follow-up is a real Google
Cloud OAuth client + test account + Ren's go-ahead).
"""

import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import config
import db
from calendar_providers import mock as mock_calendar
from calendar_providers.base import BaseCalendarProvider, CalendarProviderError
from calendar_providers.google import GoogleProvider
from calendar_providers.registry import PROVIDER_ORDER, get_provider, readiness_for
from tests.test_harness_staging_safety import _staging_mock_config


@pytest.fixture(autouse=True)
def _isolate():
    db.init_db()
    mock_calendar.clear()
    yield
    mock_calendar.clear()


def _now():
    return datetime.now(timezone.utc)


def _iso_in(hours=1):
    return (_now() + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_google_first_microsoft_not_built():
    """Google is first; Microsoft is second behind the same interface
    but not built this phase — it resolves to None, never a stub."""
    assert PROVIDER_ORDER[0] == "google"
    assert get_provider("google") is not None
    assert get_provider("microsoft") is None
    assert get_provider("unknown-provider") is None
    assert get_provider("") is None
    assert get_provider(None) is None


def test_google_readiness_reports_setup_required_without_credentials(monkeypatch):
    """With no OAuth client configured (today's state), Google reports
    setup_required — the connect button is never offered."""
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_SECRET", "")
    readiness = readiness_for("google")
    assert readiness["ready"] is False
    assert readiness["state"] == "setup_required"
    assert "not configured" in (readiness["reason"] or "")


def test_google_readiness_live_with_credentials(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_SECRET", "secret")
    readiness = readiness_for("google")
    assert readiness["ready"] is True
    assert readiness["state"] == "live"


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------


def test_mock_provider_round_trip(monkeypatch):
    """Seed -> list (windowed) -> create -> update -> delete."""
    _staging_mock_config(monkeypatch)
    provider = get_provider("mock")
    assert isinstance(provider, BaseCalendarProvider)
    assert provider.readiness()["ready"] is True
    creds = {"user_id": 7, "provider": "mock"}

    seeded = mock_calendar.seed_event(
        7, {"summary": "Seeded", "start_at": _iso_in(1), "end_at": _iso_in(2)}
    )
    assert seeded["id"].startswith("mock-")
    events = provider.list_events(
        credentials=creds, start_utc=_iso_in(0), end_utc=_iso_in(3)
    )
    assert [e["id"] for e in events] == [seeded["id"]]
    # Outside the window: not listed.
    assert provider.list_events(
        credentials=creds, start_utc=_iso_in(3), end_utc=_iso_in(4)
    ) == []

    created = provider.create_event(
        credentials=creds,
        event={"summary": "New", "start_at": _iso_in(5), "end_at": _iso_in(6)},
    )
    assert created["id"].startswith("mock-")
    moved_start, moved_end = _iso_in(7), _iso_in(8)
    updated = provider.update_event(
        credentials=creds,
        event_id=created["id"],
        event={"summary": "Moved", "start_at": moved_start, "end_at": moved_end},
    )
    assert updated["summary"] == "Moved"
    assert updated["start_at"] == moved_start
    provider.delete_event(credentials=creds, event_id=created["id"])
    assert provider.list_events(
        credentials=creds, start_utc=_iso_in(6), end_utc=_iso_in(9)
    ) == []
    assert len(mock_calendar.list_events_for(7)) == 1  # only the seed remains


def test_mock_never_resolves_without_mock_transport(two_users, monkeypatch):
    """A mock connection resolves to NO provider when the mock
    transport is off — the mock can never run in production."""
    import calendar_connections_db as cal_db

    u1, _ = two_users
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    assert cal_db.ensure_mock_connection(u1) is not None
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    provider, credentials = cal_db.resolve_calendar_provider(u1)
    assert provider is None
    assert credentials is None


def test_mock_connection_refused_without_mock_transport(two_users, monkeypatch):
    """ensure_mock_connection refuses when the mock transport is off."""
    import calendar_connections_db as cal_db

    u1, _ = two_users
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    assert cal_db.ensure_mock_connection(u1) is None


def test_mock_resolves_with_mock_transport(two_users, monkeypatch):
    import calendar_connections_db as cal_db

    u1, _ = two_users
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    conn = cal_db.ensure_mock_connection(u1)
    assert conn is not None
    assert conn["provider"] == "mock"
    provider, credentials = cal_db.resolve_calendar_provider(u1)
    assert provider is not None
    assert provider.name == "mock"
    assert credentials["user_id"] == u1


# ---------------------------------------------------------------------------
# Google OAuth shape (stubbed HTTP — no network)
# ---------------------------------------------------------------------------


def test_google_authorize_url_shape(monkeypatch):
    """The authorize URL carries the Calendar scope, offline access
    (refresh token), and prompt=consent — the shape the real OAuth
    client will need."""
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_SECRET", "secret")
    provider = GoogleProvider()
    url = provider.get_authorize_url(
        state="state-token", redirect_uri="https://app.example.com/calendar/connections/google/callback"
    )
    parsed = urllib.parse.urlparse(url)
    assert parsed.netloc == "accounts.google.com"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["client_id"] == ["id.apps.googleusercontent.com"]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["state"] == ["state-token"]
    assert "calendar.events" in query["scope"][0]


def test_google_exchange_code_shape(monkeypatch):
    """exchange_code posts the code, reads the tokens, and resolves
    the primary calendar — all against a stubbed HTTP layer."""
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_SECRET", "secret")
    provider = GoogleProvider()

    def fake_post_form(url, form):
        if "oauth2.googleapis.com/token" in url:
            assert form["grant_type"] == "authorization_code"
            assert form["code"] == "auth-code"
            return {
                "access_token": "ya29.access",
                "refresh_token": "1//refresh",
                "expires_in": 3600,
            }
        raise AssertionError(f"unexpected POST {url}")

    def fake_primary(access_token):
        assert access_token == "ya29.access"
        return {"id": "primary", "summary": "Ren Morrissey"}

    with patch.object(provider, "_post_form", side_effect=fake_post_form), patch.object(
        provider, "_primary_calendar", side_effect=fake_primary
    ):
        identity = provider.exchange_code(
            code="auth-code",
            redirect_uri="https://app.example.com/calendar/connections/google/callback",
        )
    assert identity["access_token"] == "ya29.access"
    assert identity["refresh_token"] == "1//refresh"
    assert identity["external_account_id"] == "primary"
    assert identity["display_name"] == "Ren Morrissey"
    assert identity["calendar_id"] == "primary"
    assert identity["expires_at"] is not None


def test_google_exchange_code_without_access_token_raises(monkeypatch):
    """A token response without an access token is a provider error
    with a user-facing message — never a silent empty connection."""
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_SECRET", "secret")
    provider = GoogleProvider()
    with patch.object(provider, "_post_form", return_value={}):
        with pytest.raises(CalendarProviderError) as excinfo:
            provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")
    assert "access token" in str(excinfo.value).lower()
    assert excinfo.value.user_message


def test_google_event_crud_shape(monkeypatch):
    """create/update/delete/list hit the Calendar v3 paths with the
    event payload shape — against a stubbed request layer."""
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_CLIENT_SECRET", "secret")
    provider = GoogleProvider()
    creds = {"access_token": "ya29.access", "calendar_id": "primary"}
    seen = []

    def fake_request(*, credentials, method, path, body=None):
        seen.append((method, path, body))
        if method == "POST":
            return {"id": "evt-1", "summary": "Appointment"}
        if method == "GET":
            return {"items": [{"id": "evt-1", "summary": "Appointment"}]}
        return {}

    with patch.object(provider, "_request", side_effect=fake_request):
        created = provider.create_event(
            credentials=creds,
            event={"summary": "Appointment", "start_at": _iso_in(1), "end_at": _iso_in(2)},
        )
        assert created["id"] == "evt-1"
        provider.update_event(
            credentials=creds,
            event_id="evt-1",
            event={"summary": "Moved", "start_at": _iso_in(3), "end_at": _iso_in(4)},
        )
        events = provider.list_events(
            credentials=creds, start_utc=_iso_in(0), end_utc=_iso_in(5)
        )
        assert [e["id"] for e in events] == ["evt-1"]
        provider.delete_event(credentials=creds, event_id="evt-1")
    methods_paths = [(m, p) for m, p, _b in seen]
    assert ("POST", "/calendars/primary/events") in methods_paths
    assert ("PUT", "/calendars/primary/events/evt-1") in methods_paths
    assert any(
        m == "GET" and p.startswith("/calendars/primary/events?timeMin=")
        for m, p in methods_paths
    )
    assert ("DELETE", "/calendars/primary/events/evt-1") in methods_paths
