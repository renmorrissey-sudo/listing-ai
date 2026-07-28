"""Provider-neutral SMS interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class SmsProviderError(Exception):
    """Safe SMS failure for API responses. Secrets never belong on this object."""

    def __init__(
        self,
        message,
        *,
        status_code=None,
        provider_code=None,
        more_info=None,
        message_sid=None,
        retryable=False,
        provider=None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code
        self.more_info = more_info
        self.message_sid = message_sid
        self.retryable = retryable
        self.provider = provider

    def to_public_dict(self):
        payload = {
            "error": str(self),
            "provider": self.provider or "unknown",
        }
        if self.provider_code is not None:
            payload["provider_code"] = self.provider_code
        if self.status_code is not None:
            payload["http_status"] = self.status_code
        if self.message_sid:
            payload["provider_message_id"] = self.message_sid
        return payload


class BaseSMSProvider(ABC):
    name = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def send_message(
        self,
        *,
        to_number: str,
        body: str,
        from_number: str | None = None,
        status_callback: str | None = None,
        mode: str = "AUTO",
    ) -> dict:
        ...

    def send_batch(self, messages: list) -> dict:
        """Not used — TopAI owns batching. Override only if provider preserves per-recipient IDs."""
        raise SmsProviderError(
            "Native batch send is not used. TopAI queues per-recipient sends.",
            provider=self.name,
        )

    def schedule_batch(self, *args, **kwargs):
        raise SmsProviderError(
            "Scheduling is owned by TopAI campaign workers.",
            provider=self.name,
        )

    def cancel_scheduled_batch(self, *args, **kwargs):
        raise SmsProviderError(
            "Scheduling is owned by TopAI campaign workers.",
            provider=self.name,
        )

    def get_message_status(self, provider_message_id: str) -> dict:
        return {"provider_message_id": provider_message_id, "status": "unknown"}

    def normalize_inbound_webhook(self, payload: dict) -> dict:
        raise NotImplementedError

    def normalize_delivery_webhook(self, payload: dict) -> dict:
        raise NotImplementedError

    def normalize_unsubscribe_webhook(self, payload: dict) -> dict:
        raise NotImplementedError

    def validate_webhook(self, request) -> bool:
        return True

    def supports_bulk_send(self) -> bool:
        return False

    def supports_scheduling(self) -> bool:
        return False

    def estimate_segments(self, text: str) -> dict:
        return {"encoding": "unknown", "character_count": len(text or ""), "segments": None}

    def supports_mms(self) -> bool:
        return False

    def normalize_opt_out_event(self, payload: dict) -> dict:
        return self.normalize_unsubscribe_webhook(payload)

    def supports_link_tracking(self) -> bool:
        return False

    def get_sender_information(self) -> dict:
        return {"provider": self.name, "configured": self.is_configured()}

    # Backward-compatible alias used by existing app paths
    def send_sms(self, phone_number, message_body, status_callback=None, from_number=None):
        return self.send_message(
            to_number=phone_number,
            body=message_body,
            from_number=from_number,
            status_callback=status_callback,
        )
