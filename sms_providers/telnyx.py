"""Telnyx Messaging API V2 provider."""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request

import config
from sms_providers.base import BaseSMSProvider, SmsProviderError

logger = logging.getLogger(__name__)


class TelnyxSMSProvider(BaseSMSProvider):
    name = "telnyx"

    def __init__(self):
        self.api_key = (config.TELNYX_API_KEY or "").strip()
        self.api_base = (config.TELNYX_API_BASE or "https://api.telnyx.com/v2").rstrip("/")
        self.messaging_profile_id = (config.TELNYX_MESSAGING_PROFILE_ID or "").strip()
        self.phone_number = (config.TELNYX_PHONE_NUMBER or "").strip()
        self.public_key = (config.TELNYX_PUBLIC_KEY or "").strip()
        self.trial_mode = bool(config.TELNYX_TRIAL_MODE)
        self.verified_test_number = (config.TELNYX_VERIFIED_TEST_NUMBER or "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def config_error(self):
        if not self.api_key:
            return "Telnyx API key is not configured."
        return None

    def get_sender_information(self) -> dict:
        return self.configuration_status()

    def configuration_status(self, *, latest_error_code=None, latest_error_message=None):
        """Non-secret Telnyx diagnostics for the AI SMS Assistant UI."""
        from sms_authorization import (
            get_telnyx_toll_free_verification_status,
            is_sms_sending_enabled,
            is_telnyx_toll_free_verified,
            telnyx_configuration_complete,
        )

        support_display = getattr(config, "SMS_SUPPORT_DISPLAY", None) or "(888) 821-0810"
        toll_free_e164 = (
            getattr(config, "SMS_SUPPORT_E164", None)
            or self.phone_number
            or "+18888210810"
        )
        verification = get_telnyx_toll_free_verification_status() or "unknown"
        if verification not in {
            "pending",
            "verified",
            "unknown",
            "rejected",
            "failed",
            "waiting",
            "waiting for telnyx",
            "waiting for vendor",
        }:
            # Keep raw normalized value for display; eligibility still requires exact "verified".
            pass
        app_url = (getattr(config, "APP_URL", None) or "").rstrip("/")
        webhook_configured = bool(app_url) and "localhost" not in app_url.lower()
        config_complete = telnyx_configuration_complete()
        sending_enabled = is_sms_sending_enabled()
        # Prefer public program display number; never expose API keys / public key material.
        return {
            "provider": self.name,
            "sms_provider": self.name,
            "configured": self.is_configured(),
            "send_configured": config_complete,
            "sms_sending_enabled": sending_enabled,
            "toll_free_verified": is_telnyx_toll_free_verified(),
            "api_key_configured": bool(self.api_key),
            "messaging_profile_configured": bool(self.messaging_profile_id),
            "phone_number_configured": bool(self.phone_number),
            "public_key_configured": bool(self.public_key),
            "webhook_endpoint_configured": webhook_configured,
            "webhook_api_version": "V2",
            "webhook_path": "/webhooks/telnyx/messaging",
            "toll_free_number": toll_free_e164,
            "toll_free_number_display": support_display
            if support_display.startswith("(")
            else support_display,
            "toll_free_verification_status": verification or "unknown",
            "trial_mode": bool(self.trial_mode),
            "verified_test_number_configured": bool(self.verified_test_number),
            "phone_number_hint": (self.phone_number[-4:] if self.phone_number else None),
            "verified_test_hint": (
                self.verified_test_number[-4:] if self.verified_test_number else None
            ),
            "latest_telnyx_error_code": latest_error_code,
            "latest_telnyx_error_message": (
                (latest_error_message or "")[:240] if latest_error_message else None
            ),
            "latest_error_code": latest_error_code,
            "latest_error_message": (
                (latest_error_message or "")[:240] if latest_error_message else None
            ),
        }

    def _request(self, method, path, body=None):
        # Outbound auth uses TELNYX_API_KEY only — never TELNYX_PUBLIC_KEY.
        url = f"{self.api_base}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "TopAI-Real-Estate-Tools/1.0",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if path == "/messages" and isinstance(body, dict):
                from sms_send_diagnostics import safe_telnyx_payload

                logger.info(
                    "Telnyx provider_request method=%s url=%s auth=Bearer[TELNYX_API_KEY] payload=%s",
                    method,
                    url,
                    safe_telnyx_payload(
                        from_number=body.get("from"),
                        to_number=body.get("to"),
                        text=body.get("text"),
                        messaging_profile_id=body.get("messaging_profile_id"),
                        webhook_url=body.get("webhook_url"),
                    ),
                )
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}, resp.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise self._error_from_http(detail, exc.code) from None
        except urllib.error.URLError as exc:
            logger.warning("Telnyx network error: %s", type(exc).__name__)
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
            errors = data.get("errors") or []
            if errors and isinstance(errors, list):
                first = errors[0] if isinstance(errors[0], dict) else {}
                code = first.get("code") or first.get("title")
                message = str(first.get("detail") or first.get("title") or "")
            else:
                message = str(data.get("message") or data.get("error") or "")[:180]
        except Exception:
            message = (detail or "")[:180]
        lowered = message.lower()
        retryable = http_status == 429 or http_status >= 500
        if http_status in {401, 403}:
            user = "SMS could not be sent. Messaging provider authentication failed."
            retryable = False
        elif "fund" in lowered or "balance" in lowered or "payment" in lowered:
            user = "SMS could not be sent. The messaging account has insufficient funds."
            retryable = False
        elif http_status == 429:
            user = "SMS could not be sent. Provider rate limit reached. Please retry shortly."
        elif "trial" in lowered or "verified" in lowered:
            user = "Telnyx trial messaging can only send to the verified test phone number."
            retryable = False
        else:
            user = "SMS could not be sent due to a provider error."
        logger.error("Telnyx send failed http=%s code=%s", http_status, code)
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
        if self.public_key and self.api_key and self.public_key == self.api_key:
            # Misconfiguration guard: public key must never authenticate outbound sends.
            raise SmsProviderError(
                "SMS could not be sent. Messaging provider authentication is misconfigured.",
                provider=self.name,
            )
        from_num = from_number or self.phone_number
        payload = {
            "to": to_number,
            "text": body,
            "type": "SMS",
        }
        if from_num:
            payload["from"] = from_num
        elif self.messaging_profile_id:
            payload["messaging_profile_id"] = self.messaging_profile_id
        else:
            raise SmsProviderError(
                "No SMS sender is assigned to this account.",
                provider=self.name,
            )
        if self.messaging_profile_id and "messaging_profile_id" not in payload:
            # Attach profile when using from number so profile webhooks apply
            payload["messaging_profile_id"] = self.messaging_profile_id
        if status_callback:
            payload["webhook_url"] = status_callback
            payload["use_profile_webhooks"] = True

        # Official Telnyx Messaging API V2: POST /v2/messages with Bearer API key.
        result, http_status = self._request("POST", "/messages", payload)
        data = result.get("data") or result
        provider_message_id = data.get("id")
        if not provider_message_id:
            raise SmsProviderError(
                "SMS could not be sent. SMS provider did not return a message ID.",
                status_code=http_status,
                provider=self.name,
            )
        logger.info(
            "Telnyx provider_response http=%s provider_message_id=%s to=%s from=%s",
            http_status,
            provider_message_id,
            to_number,
            from_num,
        )
        return {
            "provider_message_id": str(provider_message_id),
            "status": (data.get("to") or [{}])[0].get("status")
            if isinstance(data.get("to"), list) and data.get("to")
            else data.get("status") or "queued",
            "to": to_number,
            "from": from_num,
            "segments": (data.get("parts") or data.get("encoding") or {}).get("parts")
            if isinstance(data.get("parts"), dict)
            else data.get("parts"),
            "cost": (data.get("cost") or {}).get("amount") if isinstance(data.get("cost"), dict) else None,
            "raw_status": data.get("status"),
            "http_status": http_status,
        }

    def get_message_status(self, provider_message_id: str) -> dict:
        result, _ = self._request("GET", f"/messages/{provider_message_id}")
        data = result.get("data") or result
        return {
            "provider_message_id": provider_message_id,
            "status": data.get("status") or "unknown",
            "raw": data,
        }

    def estimate_segments(self, text: str) -> dict:
        """Rough GSM-7 vs UCS-2 segment estimate (Telnyx may also smart-encode)."""
        body = text or ""
        gsm_basic = set(
            "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
            "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
        )
        is_ucs2 = any(ch not in gsm_basic and ord(ch) > 127 for ch in body)
        length = len(body)
        if is_ucs2:
            single, multi = 70, 67
            encoding = "UCS-2"
        else:
            single, multi = 160, 153
            encoding = "GSM-7"
        if length == 0:
            segments = 0
        elif length <= single:
            segments = 1
        else:
            segments = (length + multi - 1) // multi
        return {
            "encoding": encoding,
            "character_count": length,
            "segments": segments,
            "warning_multi_segment": segments > 1,
        }

    def validate_webhook(self, request) -> bool:
        """Verify Ed25519 signature: telnyx-signature-ed25519 over `{timestamp}|{raw_body}`."""
        if not self.public_key:
            # Allow in non-production without public key for local tests only.
            return not getattr(config, "IS_PRODUCTION", False)

        signature_b64 = request.headers.get("telnyx-signature-ed25519") or request.headers.get(
            "Telnyx-Signature-Ed25519"
        )
        timestamp = request.headers.get("telnyx-timestamp") or request.headers.get("Telnyx-Timestamp")
        if not signature_b64 or not timestamp:
            return False

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        tolerance = int(getattr(config, "TELNYX_WEBHOOK_TOLERANCE_SECONDS", 300) or 300)
        if abs(int(time.time()) - ts) > tolerance:
            logger.info("Telnyx webhook rejected: stale timestamp")
            return False

        raw = request.get_data(cache=True, as_text=False) or b""
        signed = f"{timestamp}|".encode("utf-8") + raw
        try:
            from nacl.exceptions import BadSignatureError
            from nacl.signing import VerifyKey

            key_bytes = self._decode_public_key(self.public_key)
            sig_bytes = base64.b64decode(signature_b64)
            VerifyKey(key_bytes).verify(signed, sig_bytes)
            return True
        except BadSignatureError:
            logger.info("Telnyx webhook rejected: bad signature")
            return False
        except Exception:
            logger.exception("Telnyx webhook signature verification error")
            return False

    @staticmethod
    def _decode_public_key(value: str) -> bytes:
        cleaned = (value or "").strip()
        # Hex (64 chars) or base64
        if len(cleaned) == 64 and all(c in "0123456789abcdefABCDEF" for c in cleaned):
            return bytes.fromhex(cleaned)
        try:
            return base64.b64decode(cleaned)
        except Exception:
            return cleaned.encode("utf-8")

    def normalize_inbound_webhook(self, payload: dict) -> dict:
        data = (payload or {}).get("data") or payload or {}
        body = data.get("payload") or data
        from_obj = body.get("from") or {}
        to_list = body.get("to") or []
        to_phone = None
        if isinstance(to_list, list) and to_list:
            first = to_list[0]
            to_phone = first.get("phone_number") if isinstance(first, dict) else first
        elif isinstance(body.get("to"), str):
            to_phone = body.get("to")
        from_phone = from_obj.get("phone_number") if isinstance(from_obj, dict) else body.get("from")
        return {
            "event_id": data.get("id"),
            "provider_message_id": body.get("id"),
            "account_phone": to_phone,
            "contact_phone": from_phone,
            "text": body.get("text") or "",
            "timestamp": data.get("occurred_at") or body.get("received_at"),
            "media_items": body.get("media") or [],
            "raw_type": data.get("event_type") or "message.received",
        }

    def normalize_delivery_webhook(self, payload: dict) -> dict:
        data = (payload or {}).get("data") or payload or {}
        body = data.get("payload") or data
        event_type = data.get("event_type") or ""
        to_list = body.get("to") or []
        status = "unknown"
        contact = None
        error_code = None
        error_message = None
        if isinstance(to_list, list) and to_list:
            first = to_list[0] if isinstance(to_list[0], dict) else {}
            status = (first.get("status") or body.get("status") or "unknown").lower()
            contact = first.get("phone_number")
            errors = first.get("errors") or body.get("errors") or []
            if errors and isinstance(errors[0], dict):
                error_code = errors[0].get("code")
                error_message = errors[0].get("detail") or errors[0].get("title")
        from_obj = body.get("from") or {}
        account = from_obj.get("phone_number") if isinstance(from_obj, dict) else body.get("from")
        if event_type == "message.sent" and status in {"unknown", ""}:
            status = "sent"
        if event_type == "message.delivery_failed":
            status = "delivery_failed"
        return {
            "event_id": data.get("id"),
            "provider_message_id": body.get("id"),
            "account_phone": account,
            "contact_phone": contact,
            "status": status,
            "raw_type": event_type,
            "error_code": error_code,
            "error_message": error_message,
            "cost": (body.get("cost") or {}).get("amount") if isinstance(body.get("cost"), dict) else None,
            "occurred_at": data.get("occurred_at"),
        }

    def normalize_unsubscribe_webhook(self, payload: dict) -> dict:
        # Telnyx typically delivers STOP as inbound message.received with keyword text
        inbound = self.normalize_inbound_webhook(payload)
        return {
            "event_id": inbound.get("event_id"),
            "contact_phone": inbound.get("contact_phone"),
            "account_phone": inbound.get("account_phone"),
            "text": inbound.get("text"),
            "raw_type": inbound.get("raw_type"),
        }

    def supports_mms(self) -> bool:
        return True

    def supports_link_tracking(self) -> bool:
        return False  # TopAI owns branded tracking
