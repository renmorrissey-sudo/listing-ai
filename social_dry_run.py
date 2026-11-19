"""Staging-only dry-run layer for social posting (Phase A social testing).

When dry-run is enabled, ``dry_run_publish`` walks the same targeting
as the real one-click Post — the tenant's default-enabled connections,
or explicit ``connection_ids`` — and, per channel, records the exact
payload the provider WOULD send (URL, method, headers with secrets
redacted, body) plus an audit row, without calling any provider.

How the exact payload is captured
----------------------------------
The payload is captured by running the provider's real ``publish_text``
against a recording transport: a stand-in for ``urllib.request.urlopen``
that captures the ``Request`` object and returns a canned success
response. The provider code itself builds the request — URL, headers,
body, truncation — so the recorded payload is exactly what a live
publish would send. There is no parallel request-building logic to
drift out of sync with the providers.

Readiness is NOT filtered out (unlike the real publish path): a channel
that is not ready (Facebook without Meta review, X coming soon, a
missing/expired connection) still gets a dry-run row, recording the
readiness reason instead of a payload. That is the point of the layer —
it verifies generation → Post → orchestrator → per-channel readiness →
payload construction for every channel, including gated ones.

Safety
------
* ``is_dry_run_enabled()`` is True only when ``APP_ENV == "staging"``
  AND the ``SOCIAL_DRY_RUN`` env flag is set. Anywhere else it is
  False and ``dry_run_publish`` raises ``SocialDryRunNotAvailable``.
  There is no code path that enables it in production.
* Dry-run rows live in ``social_publications`` with status ``'dry_run'``
  and the payload in ``dry_run_payload_json`` (migration 043). They
  never carry a ``provider_post_id``/``provider_post_url``, so nothing
  can mistake one for a real post. ``publications_for_generations``
  (the archive status view) excludes dry-run rows, so a dry-run never
  masks a real publication status.
* Dry-run operation ids are namespaced ``dryrun-<uuid>``, so their
  idempotency keys can never collide with a real publish operation.
  Re-running the same dry-run operation returns the recorded payload
  instead of re-capturing it.
* Secrets never land in a recorded payload: the ``Authorization``
  header is redacted, ``access_token``/``client_secret`` body fields
  are redacted, and every credential value is scrubbed from the
  serialized payload before it is stored.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from unittest.mock import patch

import config
import listing_generations_db as listing_db
import social_connections_db as social_db
from social_content import caption_for_platform
from social_providers.base import SocialProviderError, SocialProviderNotAvailable
from social_providers.registry import get_provider, is_ready

logger = logging.getLogger(__name__)

DRY_RUN_OPERATION_PREFIX = "dryrun-"

# Keys that must never appear with a real value in a recorded payload.
_SENSITIVE_BODY_KEYS = {"access_token", "client_secret", "refresh_token", "token"}


class SocialDryRunNotAvailable(RuntimeError):
    """Dry-run was requested outside staging (or without the flag)."""


def is_dry_run_enabled() -> bool:
    """True only in staging with the SOCIAL_DRY_RUN flag set.

    The staging check is on the config value, not the environment at
    call time, so a production process can never enable it by flipping
    an env var after boot — and the flag alone is inert outside staging.
    """
    return bool(config.IS_STAGING and config.SOCIAL_DRY_RUN)


class _FakeHTTPResponse:
    """Minimal urlopen response: canned body + headers, context-manager."""

    def __init__(self, body: bytes = b"{}", headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingTransport:
    """Stand-in for ``urllib.request.urlopen``.

    Captures every request the provider code builds and returns a canned
    success, so no network call is ever made. Only installed inside
    ``dry_run_publish``; it is never used by the real publish path.
    """

    def __init__(self):
        self.requests: list[dict] = []

    def __enter__(self):
        self._patcher = patch("urllib.request.urlopen", side_effect=self._urlopen)
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False

    def _urlopen(self, request, timeout=None):
        if isinstance(request, urllib.request.Request):
            url = request.full_url
            method = request.get_method()
            headers = dict(request.headers)
            headers.update(getattr(request, "unredirected_hdrs", {}) or {})
            data = request.data
        else:
            url = str(request)
            method = "GET"
            headers = {}
            data = None
        self.requests.append(
            {"url": url, "method": method, "headers": headers, "data": data}
        )
        if "linkedin.com/rest/posts" in url:
            return _FakeHTTPResponse(b"{}", {"x-restli-id": "urn:li:share:dry-run"})
        if "graph.facebook.com" in url:
            return _FakeHTTPResponse(b'{"id": "dry-run-fake-post-id"}', {})
        return _FakeHTTPResponse(b"{}", {})


def _redact_value(value, secrets: set[str]):
    """Recursively redact sensitive keys and scrub secret values."""
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if key.lower() in _SENSITIVE_BODY_KEYS else _redact_value(v, secrets))
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v, secrets) for v in value]
    if isinstance(value, str):
        scrubbed = value
        for secret in secrets:
            if secret and secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "[redacted]")
        return scrubbed
    return value


def _parse_body(data: bytes | None, headers: dict):
    """Decode a captured request body into a storable value."""
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace")
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value.lower()
            break
    if "application/json" in content_type:
        try:
            return json.loads(text or "{}")
        except ValueError:
            return text
    if "application/x-www-form-urlencoded" in content_type or ("=" in text and "&" in text):
        try:
            return dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
        except ValueError:
            return text
    return text


def _build_payload(provider_name, request, caption, connection, credentials):
    """The exact request the provider would send, with secrets redacted."""
    secrets = {
        str(v)
        for v in (credentials.get("access_token"), credentials.get("refresh_token"))
        if v
    }
    headers = {
        key: ("[redacted]" if key.lower() == "authorization" else value)
        for key, value in request["headers"].items()
    }
    body = _redact_value(_parse_body(request["data"], request["headers"]), secrets)
    payload = {
        "provider": provider_name,
        "method": request["method"],
        "url": request["url"],
        "headers": _redact_value(headers, secrets),
        "body": body,
        "caption": caption,
        "connection_display_name": connection.get("display_name"),
        "external_account_id": connection.get("external_account_id"),
    }
    # Belt-and-braces: scrub any secret value that survived structural
    # redaction anywhere in the serialized payload.
    return _redact_value(payload, secrets)


def _dry_run_view(publication, connection, payload, note=None):
    return {
        "provider": publication["provider"],
        "status": "dry_run",
        "payload": payload,
        "note": note if note is not None else publication.get("error_summary"),
        "connection_display_name": connection.get("display_name"),
        "publication_id": publication["id"],
    }


def _record_dry_run(publication, *, payload=None, note=None, error_code=None):
    """Write the dry-run outcome onto the publication row (the audit record)."""
    payload_json = json.dumps(payload) if payload is not None else None
    social_db.update_publication_result(
        publication["id"],
        status="dry_run",
        error_code=error_code,
        error_summary=note,
        dry_run_payload_json=payload_json,
    )
    publication["status"] = "dry_run"
    publication["error_summary"] = note
    publication["dry_run_payload_json"] = payload_json


def _dry_run_one(user_id, generation, connection, operation_id):
    provider_name = connection["provider"]
    idem_key = social_db.build_idempotency_key(operation_id, provider_name)
    publication = social_db.create_publication(
        user_id, generation["id"], provider_name, connection["id"], idem_key
    )

    # Idempotency: this exact dry-run operation already recorded — return
    # the recorded payload, never re-capture or re-call anything.
    if publication["status"] == "dry_run":
        payload = None
        raw = publication.get("dry_run_payload_json")
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
        return _dry_run_view(publication, connection, payload)
    if publication["status"] in ("published", "failed"):
        # A real publication already resolved under this key. Dry-run
        # keys are namespaced so this should not happen; never overwrite it.
        return _dry_run_view(
            publication,
            connection,
            None,
            note="A real publication already exists for this operation.",
        )

    provider = get_provider(provider_name)
    if not provider or not is_ready(provider_name):
        readiness = provider.readiness() if provider else {}
        reason = readiness.get("reason") or (
            f"{connection.get('display_name') or provider_name} isn't available for posting yet."
        )
        _record_dry_run(publication, note=reason)
        logger.info(
            "Social dry-run user=%s generation=%s provider=%s publication=%s gated=%s",
            user_id, generation["id"], provider_name, publication["id"], reason,
        )
        return _dry_run_view(publication, connection, None, note=reason)

    credentials = social_db.get_connection_credentials(user_id, connection["id"])
    if not credentials or not credentials.get("access_token"):
        reason = (
            f"Your {provider.display_name} connection has expired. "
            f"Reconnect {provider.display_name}."
        )
        social_db.mark_connection_needs_reconnect(connection["id"])
        _record_dry_run(publication, note=reason)
        return _dry_run_view(publication, connection, None, note=reason)

    caption = caption_for_platform(generation.get("social_content"), provider_name)
    transport = _RecordingTransport()
    try:
        with transport:
            provider.publish_text(
                credentials=credentials, caption=caption, listing_generation=generation
            )
    except SocialProviderNotAvailable as exc:
        reason = exc.user_message
        _record_dry_run(publication, note=reason)
        logger.info(
            "Social dry-run user=%s generation=%s provider=%s publication=%s unavailable=%s",
            user_id, generation["id"], provider_name, publication["id"], reason,
        )
        return _dry_run_view(publication, connection, None, note=reason)
    except SocialProviderError as exc:
        if exc.needs_reconnect:
            social_db.mark_connection_needs_reconnect(connection["id"])
        _record_dry_run(publication, note=exc.user_message, error_code=exc.error_code)
        return _dry_run_view(publication, connection, None, note=exc.user_message)
    except Exception:
        logger.exception(
            "Social dry-run failed provider=%s generation=%s",
            provider_name, generation["id"],
        )
        reason = "Something went wrong building this post. Try again."
        _record_dry_run(publication, note=reason)
        return _dry_run_view(publication, connection, None, note=reason)

    if len(transport.requests) != 1:
        reason = (
            "Expected the provider to make exactly one publish request; "
            f"captured {len(transport.requests)}."
        )
        _record_dry_run(publication, note=reason)
        return _dry_run_view(publication, connection, None, note=reason)

    payload = _build_payload(
        provider_name, transport.requests[0], caption, connection, credentials
    )
    _record_dry_run(publication, payload=payload)
    logger.info(
        "Social dry-run user=%s generation=%s provider=%s publication=%s payload_recorded=true",
        user_id, generation["id"], provider_name, publication["id"],
    )
    return _dry_run_view(publication, connection, payload)


def dry_run_publish(user_id, listing_generation_id, *, operation_id=None, connection_ids=None):
    """Staging-only dry-run of the one-click Post.

    Returns {"operation_id", "dry_run": True, "results": [...]} where each
    result is a per-provider {"provider", "status": "dry_run",
    "payload" (exact request the provider would send, or None when the
    channel is gated), "note", ...}. Raises SocialDryRunNotAvailable
    outside staging (or without the SOCIAL_DRY_RUN flag) and ValueError
    for an unknown generation. Never calls a provider.
    """
    if not is_dry_run_enabled():
        raise SocialDryRunNotAvailable(
            "Social dry-run is only available in staging with SOCIAL_DRY_RUN enabled."
        )
    generation = listing_db.get_by_id(user_id, listing_generation_id)
    if not generation:
        raise ValueError("Listing not found or no longer retained.")

    operation_id = DRY_RUN_OPERATION_PREFIX + (operation_id or social_db.new_operation_id())

    if connection_ids:
        wanted = {int(cid) for cid in connection_ids}
        targets = [
            c
            for c in social_db.list_connections(user_id)
            if c["id"] in wanted and c["status"] == "connected"
        ]
    else:
        # Deliberately NOT filtered by readiness: gated channels get a
        # dry-run row recording the gate reason instead of a payload.
        targets = social_db.list_default_enabled_connections(user_id)

    if not targets:
        return {"operation_id": operation_id, "dry_run": True, "results": []}

    results = [_dry_run_one(user_id, generation, connection, operation_id) for connection in targets]
    return {"operation_id": operation_id, "dry_run": True, "results": results}


def get_dry_run_record(user_id, publication_id):
    """Fetch a recorded dry-run row (tenant-scoped) with its payload parsed.

    Returns None unless the row belongs to the user and is a dry-run.
    """
    with social_db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM social_publications WHERE id = ? AND user_id = ?",
            (publication_id, user_id),
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    if record.get("status") != "dry_run":
        return None
    raw = record.get("dry_run_payload_json")
    record["payload"] = json.loads(raw) if raw else None
    return record
