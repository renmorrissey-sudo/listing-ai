"""SMS provider factory."""

from __future__ import annotations

import config
from sms_providers.base import SmsProviderError
from sms_providers.simpletexting import SimpleTextingSMSProvider
from sms_providers.twilio_adapter import TwilioSMSProvider


def get_sms_provider():
    provider = (config.SMS_PROVIDER or "twilio").lower().strip()
    if provider == "simpletexting":
        return SimpleTextingSMSProvider()
    if provider == "twilio":
        return TwilioSMSProvider()
    raise SmsProviderError(f"Unsupported SMS provider: {provider}")
