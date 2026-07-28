"""Authenticated tenant webhook ingestion for external leads."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

import external_leads_db as xdb
from external_leads.ingest import ingest_external_lead

logger = logging.getLogger(__name__)


def hash_webhook_secret(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def verify_webhook_secret(provided: str, stored_hash: str) -> bool:
    if not provided or not stored_hash:
        return False
    digest = hash_webhook_secret(provided)
    return hmac.compare_digest(digest, stored_hash)


def normalize_webhook_payload(body: dict) -> dict:
    """Map common provider-neutral keys into ingest payload."""
    if not isinstance(body, dict):
        return {}
    payload = {
        "first_name": body.get("first_name") or body.get("firstName"),
        "last_name": body.get("last_name") or body.get("lastName"),
        "full_name": body.get("full_name") or body.get("name") or body.get("lead_name"),
        "phone": body.get("phone") or body.get("phone_number") or body.get("mobile"),
        "email": body.get("email"),
        "external_record_id": (
            body.get("external_record_id")
            or body.get("record_id")
            or body.get("lead_id")
            or body.get("id")
        ),
        "property_address": body.get("property_address") or body.get("address"),
        "property_url": body.get("property_url") or body.get("listing_url"),
        "inquiry_notes": body.get("inquiry_notes") or body.get("notes") or body.get("message"),
        "lead_type": body.get("lead_type"),
        "original_consent_status": body.get("consent_status") or body.get("sms_consent"),
        "original_consent_date": body.get("consent_date"),
        "original_consent_text": body.get("consent_text"),
        "raw_payload": body,
    }
    return payload


def process_webhook(provider_key: str, body: dict, provided_secret: str):
    """
    Resolve tenant by provider_key + secret match among active sources.
    Idempotent via external_record_id when present.
    """
    candidates = xdb.find_source_by_webhook_provider_key(provider_key)
    source = None
    for cand in candidates:
        if verify_webhook_secret(provided_secret, cand.get("webhook_secret_hash") or ""):
            source = cand
            break
    if not source:
        logger.info("webhook_auth_failed provider_key=%s", provider_key)
        return None, "Invalid webhook credentials.", 401

    payload = normalize_webhook_payload(body or {})
    result = ingest_external_lead(
        source["user_id"],
        payload,
        source_row=source,
        method="webhook",
        actor_user_id=source["user_id"],
    )
    if result.get("error"):
        return result, result["error"], 400
    logger.info(
        "webhook_ingest provider_key=%s user=%s lead=%s action=%s",
        provider_key,
        source["user_id"],
        result.get("lead_id"),
        result.get("action"),
    )
    return result, None, 200
