"""Mock SMS provider for staging / tests.

Records every send in memory and returns a fake success — it never
makes a network call, never authenticates to Telnyx/Twilio, and never
delivers a real message. Selected by :mod:`sms_providers.factory`
when ``config.HARNESS_MOCK_PROVIDERS`` is true (on by default when
``APP_ENV=staging``).

The recorded sends are the staging demo's proof: the demo asserts
exactly one mock send per approved draft and zero sends in Drafts Only
without approval by inspecting :func:`sent_messages`.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone

from sms_providers.base import BaseSMSProvider

logger = logging.getLogger(__name__)

_SENT: list[dict] = []
_COUNTER = itertools.count(1)


def sent_messages() -> list[dict]:
    """Copies of every send recorded by the mock provider, in order."""
    return [dict(entry) for entry in _SENT]


def clear_sent_messages() -> None:
    """Drop all recorded sends (test/demo setup)."""
    _SENT.clear()


class MockSMSProvider(BaseSMSProvider):
    """In-memory SMS provider. No network, no real delivery."""

    name = "mock"

    def is_configured(self) -> bool:
        return True

    def send_message(
        self,
        *,
        to_number: str,
        body: str,
        from_number: str | None = None,
        status_callback: str | None = None,
        mode: str = "AUTO",
    ) -> dict:
        to_num = (to_number or "").strip()
        text = (body or "").strip()
        if not to_num:
            from sms_providers.base import SmsProviderError

            raise SmsProviderError("Mock SMS: destination number is empty.", provider=self.name)
        if not text:
            from sms_providers.base import SmsProviderError

            raise SmsProviderError("Mock SMS: message body is empty.", provider=self.name)
        message_id = f"mock-{next(_COUNTER)}"
        entry = {
            "provider_message_id": message_id,
            "message_id": message_id,
            "to": to_num,
            "from": (from_number or "mock-sender").strip(),
            "body": text,
            "status": "mock_sent",
            "status_callback": status_callback,
            "mode": mode,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        _SENT.append(entry)
        logger.info(
            "Mock SMS recorded (no real send) to=%s message_id=%s", to_num, message_id
        )
        return {
            "provider_message_id": message_id,
            "status": "mock_sent",
            "to": to_num,
            "from": entry["from"],
            "segments": 1,
            "cost": None,
            "raw_status": "mock_sent",
        }

    def get_message_status(self, provider_message_id: str) -> dict:
        for entry in _SENT:
            if entry["provider_message_id"] == provider_message_id:
                return {
                    "provider_message_id": provider_message_id,
                    "status": "mock_sent",
                    "raw": dict(entry),
                }
        return {
            "provider_message_id": provider_message_id,
            "status": "unknown",
            "raw": {},
        }
