"""SMS provider factory."""

from __future__ import annotations

import config
from sms_providers.base import SmsProviderError
from sms_providers.mock import MockSMSProvider
from sms_providers.simpletexting import SimpleTextingSMSProvider
from sms_providers.telnyx import TelnyxSMSProvider
from sms_providers.twilio_adapter import TwilioSMSProvider


def get_sms_provider():
    # Staging safety: mock providers intercept every SMS send before any
    # real provider is constructed, so a staging process can never reach
    # Telnyx/Twilio/SimpleTexting even with real keys configured. See
    # config.HARNESS_MOCK_PROVIDERS (on by default in staging).
    if getattr(config, "HARNESS_MOCK_PROVIDERS", False):
        return MockSMSProvider()
    provider = (config.SMS_PROVIDER or "twilio").lower().strip()
    if provider == "telnyx":
        return TelnyxSMSProvider()
    if provider == "simpletexting":
        return SimpleTextingSMSProvider()
    if provider == "twilio":
        return TwilioSMSProvider()
    raise SmsProviderError(f"Unsupported SMS provider: {provider}")
