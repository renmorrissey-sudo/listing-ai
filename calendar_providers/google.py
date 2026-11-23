"""Google Calendar provider (Phase III-4).

First calendar integration, behind the one provider interface. Raw
``urllib``, no new dependency. 3-legged OAuth with a single scope
(``calendar.events`` — read + write events on the user's calendars);
refresh tokens are long-lived (Google refresh tokens stay valid until
revoked, barring the 6-month inactivity rule). The Calendar API event
model maps 1:1 onto the ``appointments`` row.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
from calendar_providers.base import (
    CalendarProviderError,
    CalendarProviderNotAvailable,
    BaseCalendarProvider,
)

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar.events"


class GoogleProvider(BaseCalendarProvider):
    name = "google"
    display_name = "Google Calendar"

    def is_app_configured(self) -> bool:
        return bool(
            (config.GOOGLE_CALENDAR_CLIENT_ID or "").strip()
            and (config.GOOGLE_CALENDAR_CLIENT_SECRET or "").strip()
        )

    def readiness(self) -> dict:
        if self.is_app_configured():
            return {"state": "live", "ready": True, "reason": None}
        return {
            "state": "setup_required",
            "ready": False,
            "reason": "Google Calendar app credentials are not configured yet.",
        }

    def get_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "response_type": "code",
            "client_id": config.GOOGLE_CALENDAR_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            # Offline access: Google only issues a refresh token when
            # access_type=offline is requested; prompt=consent forces
            # a fresh grant so a reconnect always yields one.
            "access_type": "offline",
            "prompt": "consent",
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
            logger.error("Google token exchange failed http=%s detail=%s", exc.code, detail[:300])
            raise CalendarProviderError(
                "Google couldn't complete the connection. Please try again.",
                provider=self.name,
            ) from None

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        payload = self._post_form(
            TOKEN_URL,
            {
                "code": code,
                "client_id": config.GOOGLE_CALENDAR_CLIENT_ID,
                "client_secret": config.GOOGLE_CALENDAR_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        access_token = payload.get("access_token")
        if not access_token:
            raise CalendarProviderError(
                "Google didn't return an access token. Please try again.",
                provider=self.name,
            )
        expires_at = None
        expires_in = payload.get("expires_in")
        if expires_in:
            try:
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
                ).isoformat()
            except (TypeError, ValueError):
                expires_at = None
        identity = self._primary_calendar(access_token)
        return {
            "access_token": access_token,
            "refresh_token": payload.get("refresh_token"),
            "expires_at": expires_at,
            "external_account_id": identity.get("id") or "primary",
            "display_name": identity.get("summary") or "Google Calendar",
            "scopes": SCOPES,
            "calendar_id": identity.get("id") or "primary",
        }

    def _primary_calendar(self, access_token: str) -> dict:
        """The user's primary calendar (its id is the Google account email)."""
        url = f"{CALENDAR_API}/users/me/calendarList?minAccessRole=owner"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {access_token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error("Google calendarList failed http=%s detail=%s", exc.code, detail[:300])
            raise CalendarProviderError(
                "Google couldn't read the calendar list. Please try again.",
                provider=self.name,
            ) from None
        for item in data.get("items") or []:
            if item.get("primary"):
                return {"id": item.get("id"), "summary": item.get("summary")}
        items = data.get("items") or []
        if items:
            return {"id": items[0].get("id"), "summary": items[0].get("summary")}
        return {"id": "primary", "summary": "Google Calendar"}

    def refresh_access_token(self, *, refresh_token: str) -> dict:
        payload = self._post_form(
            TOKEN_URL,
            {
                "refresh_token": refresh_token,
                "client_id": config.GOOGLE_CALENDAR_CLIENT_ID,
                "client_secret": config.GOOGLE_CALENDAR_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
        )
        access_token = payload.get("access_token")
        if not access_token:
            raise CalendarProviderError(
                "Google couldn't refresh the connection. Please reconnect.",
                provider=self.name,
                needs_reconnect=True,
            )
        expires_at = None
        expires_in = payload.get("expires_in")
        if expires_in:
            try:
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
                ).isoformat()
            except (TypeError, ValueError):
                expires_at = None
        return {"access_token": access_token, "expires_at": expires_at}

    def _request(self, *, credentials: dict, method: str, path: str, body=None):
        token = (credentials or {}).get("access_token") or ""
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{CALENDAR_API}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.error(
                "Google Calendar API failed method=%s path=%s http=%s detail=%s",
                method, path, exc.code, detail[:300],
            )
            if exc.code in (401, 403):
                raise CalendarProviderError(
                    "Google Calendar connection expired. Please reconnect.",
                    provider=self.name,
                    needs_reconnect=True,
                    error_code=str(exc.code),
                ) from None
            raise CalendarProviderError(
                "Google Calendar couldn't complete that request. Please try again.",
                provider=self.name,
                retryable=exc.code in (429, 500, 502, 503, 504),
                error_code=str(exc.code),
            ) from None

    @staticmethod
    def _event_payload(event: dict) -> dict:
        return {
            "summary": str(event.get("summary") or "Appointment"),
            "description": str(event.get("description") or ""),
            "start": {"dateTime": event["start_at"]},
            "end": {"dateTime": event["end_at"]},
        }

    @staticmethod
    def _normalize_event(item: dict) -> dict:
        start = (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date")
        end = (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date")
        # All-day (date-only) events block the whole day in UTC; the
        # collision check treats them as a full-day busy block.
        if start and "T" not in str(start):
            start = f"{start}T00:00:00+00:00"
        if end and "T" not in str(end):
            end = f"{end}T23:59:59+00:00"
        return {
            "id": item.get("id"),
            "summary": item.get("summary"),
            "start_at": start,
            "end_at": end,
        }

    def list_events(self, *, credentials: dict, start_utc: str, end_utc: str) -> list:
        calendar_id = (credentials or {}).get("calendar_id") or "primary"
        params = {
            "timeMin": start_utc,
            "timeMax": end_utc,
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        path = (
            f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
            f"?{urllib.parse.urlencode(params)}"
        )
        data = self._request(credentials=credentials, method="GET", path=path)
        return [self._normalize_event(item) for item in data.get("items") or []]

    def create_event(self, *, credentials: dict, event: dict) -> dict:
        calendar_id = (credentials or {}).get("calendar_id") or "primary"
        path = f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        data = self._request(
            credentials=credentials,
            method="POST",
            path=path,
            body=self._event_payload(event),
        )
        return self._normalize_event(data)

    def update_event(self, *, credentials: dict, event_id: str, event: dict) -> dict:
        calendar_id = (credentials or {}).get("calendar_id") or "primary"
        path = (
            f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
            f"/{urllib.parse.quote(event_id, safe='')}"
        )
        data = self._request(
            credentials=credentials,
            method="PUT",
            path=path,
            body=self._event_payload(event),
        )
        return self._normalize_event(data)

    def delete_event(self, *, credentials: dict, event_id: str) -> None:
        calendar_id = (credentials or {}).get("calendar_id") or "primary"
        path = (
            f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
            f"/{urllib.parse.quote(event_id, safe='')}"
        )
        self._request(credentials=credentials, method="DELETE", path=path)
