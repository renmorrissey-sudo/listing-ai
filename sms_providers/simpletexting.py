"""SimpleTexting API v2 provider. Master token + per-tenant accountPhone."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

import config
from sms_providers.base import BaseSMSProvider, SmsProviderError

logger = logging.getLogger(__name__)


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


class SimpleTextingSMSProvider(BaseSMSProvider):
    name = "simpletexting"

    def __init__(self):
        self.api_token = (config.SIMPLETEXTING_API_TOKEN or "").strip()
        self.api_base = (config.SIMPLETEXTING_API_BASE or "").rstrip("/")
        self.webhook_secret = (config.SIMPLETEXTING_WEBHOOK_SECRET or "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_token)

    def config_error(self):
        if not self.api_token:
            return "SimpleTexting API token is not configured."
        return None

    def get_sender_information(self) -> dict:
        return {
            "provider": self.name,
            "configured": self.is_configured(),
            "api_base": self.api_base,
            "token_configured": bool(self.api_token),
            "pilot_fallback_number_configured": bool(config.SIMPLETEXTING_PHONE_NUMBER),
        }

    def _request(self, method, path, body=None):
        url = f"{self.api_base}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "User-Agent": "TopAI-Real-Estate-Tools/1.0",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}, resp.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise self._error_from_http(detail, exc.code) from None
        except urllib.error.URLError as exc:
            logger.warning("SimpleTexting network error: %s", type(exc).__name__)
            raise SmsProviderError(
                "SMS could not be sent. Could not reach the SMS provider. Please try again.",
                retryable=True,
                provider=self.name,
            ) from None

    def _error_from_http(self, detail, http_status):
        code = None
        message = ""
        try:
            data = json.loads(detail or "{}")
            code = data.get("code") or data.get("errorCode") or data.get("status")
            message = str(data.get("message") or data.get("error") or data.get("errorMessage") or "")
        except Exception:
            message = (detail or "")[:180]
        lowered = message.lower()
        retryable = http_status == 429 or http_status >= 500
        if "credit" in lowered or "balance" in lowered:
            user = "SMS could not be sent. The messaging account does not have sufficient credits."
            retryable = False
        elif http_status == 401 or http_status == 403:
            user = "SMS could not be sent. Messaging provider authentication failed."
            retryable = False
        elif http_status == 429:
            user = "SMS could not be sent. Provider rate limit reached. Please retry shortly."
        else:
            user = "SMS could not be sent due to a provider error."
        logger.error(
            "SimpleTexting send failed http=%s code=%s",
            http_status,
            code,
        )
        return SmsProviderError(
            user,
            status_code=http_status,
            provider_code=code,
            more_info=message[:200] if message else None,
            retryable=retryable,
            provider=self.name,
        )

    def send_message(
        self,
        *,
        to_number: str,
        body: str,
        from_number: str | None = None,
        status_callback: str | None = None,
        mode: str = "AUTO",
    ) -> dict:
        err = self.config_error()
        if err:
            raise SmsProviderError(err, provider=self.name)
        if not from_number:
            raise SmsProviderError(
                "SMS is not activated for this account. Assign and verify a sender number before sending.",
                provider=self.name,
            )
        payload = {
            "contactPhone": _digits(to_number),
            "accountPhone": _digits(from_number),
            "mode": mode or "AUTO",
            "text": body,
        }
        result, _status = self._request("POST", "/api/messages", payload)
        provider_message_id = result.get("id") or result.get("messageId")
        if not provider_message_id:
            raise SmsProviderError(
                "SMS could not be sent. SMS provider did not return a message ID.",
                provider=self.name,
            )
        return {
            "provider_message_id": str(provider_message_id),
            "status": result.get("status") or "submitted",
            "to": to_number,
            "from": from_number,
            "credits": result.get("credits"),
            "raw_status": result.get("status"),
        }

    def evaluate_message(self, text: str, *, to_number: str | None = None, from_number: str | None = None):
        err = self.config_error()
        if err:
            raise SmsProviderError(err, provider=self.name)
        body = {"text": text, "mode": "AUTO"}
        if to_number:
            body["contactPhone"] = _digits(to_number)
        if from_number:
            body["accountPhone"] = _digits(from_number)
        result, _ = self._request("POST", "/api/messages/evaluate", body)
        return result

    def get_message_status(self, provider_message_id: str) -> dict:
        result, _ = self._request("GET", f"/api/messages/{provider_message_id}")
        return {
            "provider_message_id": provider_message_id,
            "status": result.get("status") or "unknown",
            "raw": {k: result.get(k) for k in ("contactPhone", "accountPhone", "directionType") if k in result},
        }

    def list_phones(self):
        result, _ = self._request("GET", "/api/phones")
        return result

    def get_tenant_info(self):
        result, _ = self._request("GET", "/api/tenant")
        return result

    def validate_webhook(self, request) -> bool:
        if not self.webhook_secret:
            # Allow in development without secret; production should set it.
            return not getattr(config, "IS_PRODUCTION", False)
        token = request.args.get("token") or request.headers.get("X-TopAI-Webhook-Token") or ""
        return token == self.webhook_secret

    def normalize_inbound_webhook(self, payload: dict) -> dict:
        values = (payload or {}).get("values") or payload or {}
        return {
            "event_id": (payload or {}).get("reportId") or values.get("messageId"),
            "provider_message_id": values.get("messageId"),
            "account_phone": values.get("accountPhone"),
            "contact_phone": values.get("contactPhone") or values.get("from"),
            "text": values.get("text") or "",
            "timestamp": values.get("timestamp"),
            "category": values.get("category"),
            "media_items": values.get("mediaItems") or [],
            "raw_type": (payload or {}).get("type") or "INCOMING_MESSAGE",
        }

    def normalize_delivery_webhook(self, payload: dict) -> dict:
        values = (payload or {}).get("values") or payload or {}
        event_type = (payload or {}).get("type") or ""
        status = "delivered"
        if event_type == "NON_DELIVERED_REPORT" or str(values.get("status") or "").lower() in {
            "undelivered",
            "failed",
            "rejected",
        }:
            status = "undelivered"
        elif values.get("status"):
            status = str(values.get("status")).lower()
        return {
            "event_id": (payload or {}).get("reportId") or values.get("messageId"),
            "provider_message_id": values.get("messageId") or values.get("id"),
            "account_phone": values.get("accountPhone"),
            "contact_phone": values.get("contactPhone") or values.get("destination"),
            "status": status,
            "carrier": values.get("carrier"),
            "raw_type": event_type,
        }

    def normalize_unsubscribe_webhook(self, payload: dict) -> dict:
        values = (payload or {}).get("values") or payload or {}
        return {
            "event_id": (payload or {}).get("reportId"),
            "contact_phone": values.get("phone") or values.get("contactPhone"),
            "contact_id": values.get("contactId"),
            "account_phone": values.get("accountPhone"),
            "raw_type": (payload or {}).get("type") or "UNSUBSCRIBE_REPORT",
        }

    def supports_link_tracking(self) -> bool:
        return False  # TopAI owns branded tracking links
