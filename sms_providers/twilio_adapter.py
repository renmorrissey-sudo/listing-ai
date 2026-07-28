"""Twilio adapter wrapping legacy TwilioSmsProvider (inactive when not selected)."""

from __future__ import annotations

from sms_provider import TwilioSmsProvider as LegacyTwilio
from sms_providers.base import BaseSMSProvider, SmsProviderError


class TwilioSMSProvider(BaseSMSProvider):
    name = "twilio"

    def __init__(self):
        self._inner = LegacyTwilio()

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    def config_error(self):
        return self._inner.config_error()

    def configuration_status(self, **kwargs):
        return self._inner.configuration_status(**kwargs)

    def send_message(
        self,
        *,
        to_number: str,
        body: str,
        from_number: str | None = None,
        status_callback: str | None = None,
        mode: str = "AUTO",
    ) -> dict:
        # Legacy Twilio uses Messaging Service or configured From; from_number ignored unless needed later.
        try:
            return self._inner.send_sms(
                to_number, body, status_callback=status_callback, from_number=from_number
            )
        except Exception as exc:
            if isinstance(exc, SmsProviderError):
                raise
            # Re-wrap legacy SmsProviderError from sms_provider module
            from sms_provider import SmsProviderError as LegacyErr

            if isinstance(exc, LegacyErr):
                raise SmsProviderError(
                    str(exc),
                    status_code=getattr(exc, "status_code", None),
                    provider_code=getattr(exc, "provider_code", None),
                    more_info=getattr(exc, "more_info", None),
                    message_sid=getattr(exc, "message_sid", None),
                    provider=self.name,
                ) from None
            raise SmsProviderError(str(exc), provider=self.name) from None

    def get_sender_information(self) -> dict:
        return {
            "provider": self.name,
            "configured": self.is_configured(),
            **self._inner.configuration_status(),
        }

    def normalize_inbound_webhook(self, payload: dict) -> dict:
        return {
            "event_id": (payload or {}).get("MessageSid"),
            "provider_message_id": (payload or {}).get("MessageSid"),
            "account_phone": (payload or {}).get("To"),
            "contact_phone": (payload or {}).get("From"),
            "text": (payload or {}).get("Body") or "",
            "raw_type": "twilio_inbound",
        }

    def normalize_delivery_webhook(self, payload: dict) -> dict:
        return {
            "event_id": (payload or {}).get("MessageSid"),
            "provider_message_id": (payload or {}).get("MessageSid"),
            "status": (payload or {}).get("MessageStatus") or "unknown",
            "contact_phone": (payload or {}).get("To"),
            "raw_type": "twilio_status",
        }

    def normalize_unsubscribe_webhook(self, payload: dict) -> dict:
        return {
            "contact_phone": (payload or {}).get("From"),
            "account_phone": (payload or {}).get("To"),
            "raw_type": "twilio_opt_out",
        }
