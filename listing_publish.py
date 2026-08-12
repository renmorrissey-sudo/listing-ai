"""One-click social Post orchestrator + per-channel retry.

Publishing is an explicit, separate action from generation (generating a
listing never triggers a post). Each publish OPERATION (one click of the
main Post button, or one click of a single provider's Retry) is identified
by an `operation_id`. Retrying the exact same operation_id — double click,
browser retry, background-job retry — resolves to the same
`social_publications` row via `social_connections_db.create_publication`'s
existing-row lookup, so it can never create a duplicate post. A genuinely
new Post/Retry click always gets a fresh operation_id from the caller
(routes in app.py), so intentionally posting again later is unaffected.
"""

from __future__ import annotations

import logging

import listing_generations_db as listing_db
import social_connections_db as social_db
from social_content import caption_for_platform
from social_providers.base import SocialProviderError
from social_providers.registry import get_provider, is_ready

logger = logging.getLogger(__name__)


def _user_facing_error(exc: Exception):
    if isinstance(exc, SocialProviderError):
        return exc.user_message, exc.error_code, exc.needs_reconnect
    return "Something went wrong publishing this post. Try again.", None, False


def default_target_connections(user_id):
    """Connected + enabled-for-one-click-post + provider-ready channels for this tenant."""
    enabled = social_db.list_default_enabled_connections(user_id)
    return [c for c in enabled if is_ready(c["provider"])]


def publish_listing(user_id, listing_generation_id, *, operation_id=None, connection_ids=None):
    """One-click Post. Publishes to explicit `connection_ids` if given, else the
    tenant's default-enabled+ready channels. Returns a list of per-provider results.
    Never raises for individual provider failures — those come back as
    per-item {"status": "failed", ...} so partial success can be reported.
    """
    generation = listing_db.get_by_id(user_id, listing_generation_id)
    if not generation:
        raise ValueError("Listing not found or no longer retained.")

    operation_id = operation_id or social_db.new_operation_id()

    if connection_ids:
        wanted = {int(cid) for cid in connection_ids}
        targets = [
            c
            for c in social_db.list_connections(user_id)
            if c["id"] in wanted and c["status"] == "connected"
        ]
    else:
        targets = default_target_connections(user_id)

    if not targets:
        return {"operation_id": operation_id, "results": []}

    results = [_publish_one(user_id, generation, connection, operation_id) for connection in targets]
    return {"operation_id": operation_id, "results": results}


def retry_publish(user_id, listing_generation_id, provider, *, operation_id=None):
    """Re-attempt exactly one provider's publication without touching the others."""
    generation = listing_db.get_by_id(user_id, listing_generation_id)
    if not generation:
        raise ValueError("Listing not found or no longer retained.")
    connection = social_db.get_active_connection(user_id, provider)
    if not connection:
        raise ValueError(f"No connected {provider} account.")
    operation_id = operation_id or social_db.new_operation_id()
    return _publish_one(user_id, generation, connection, operation_id)


def _publish_one(user_id, generation, connection, operation_id):
    provider_name = connection["provider"]
    idem_key = social_db.build_idempotency_key(operation_id, provider_name)
    publication = social_db.create_publication(
        user_id, generation["id"], provider_name, connection["id"], idem_key
    )

    # Idempotency: this exact operation already resolved — never repeat the network call.
    if publication["status"] in ("published", "failed"):
        return _publication_view(publication, connection)

    provider = get_provider(provider_name)
    if not provider or not is_ready(provider_name):
        message = f"{connection.get('display_name') or provider_name} isn't available for posting yet."
        social_db.update_publication_result(publication["id"], status="failed", error_summary=message)
        publication["status"], publication["error_summary"] = "failed", message
        return _publication_view(publication, connection)

    credentials = social_db.get_connection_credentials(user_id, connection["id"])
    if not credentials or not credentials.get("access_token"):
        message = f"Your {provider.display_name} connection has expired. Reconnect {provider.display_name}."
        social_db.mark_connection_needs_reconnect(connection["id"])
        social_db.update_publication_result(publication["id"], status="failed", error_summary=message)
        publication["status"], publication["error_summary"] = "failed", message
        return _publication_view(publication, connection)

    caption = caption_for_platform(generation.get("social_content"), provider_name)
    try:
        result = provider.publish_text(credentials=credentials, caption=caption, listing_generation=generation)
    except Exception as exc:
        message, error_code, needs_reconnect = _user_facing_error(exc)
        if needs_reconnect:
            social_db.mark_connection_needs_reconnect(connection["id"])
        logger.warning(
            "Social publish failed provider=%s generation=%s: %s", provider_name, generation["id"], exc
        )
        social_db.update_publication_result(
            publication["id"], status="failed", error_code=error_code, error_summary=message
        )
        publication["status"], publication["error_summary"] = "failed", message
        return _publication_view(publication, connection)

    social_db.update_publication_result(
        publication["id"],
        status="published",
        provider_post_id=result.get("provider_post_id"),
        provider_post_url=result.get("provider_post_url"),
    )
    publication["status"] = "published"
    publication["provider_post_id"] = result.get("provider_post_id")
    publication["provider_post_url"] = result.get("provider_post_url")
    return _publication_view(publication, connection)


def _publication_view(publication, connection):
    return {
        "provider": publication["provider"],
        "status": publication["status"],
        "provider_post_url": publication.get("provider_post_url"),
        "error_summary": publication.get("error_summary"),
        "connection_display_name": connection.get("display_name"),
    }


def annotate_publish_status(user_id, generations):
    """Attach {publish_status: {provider: status}} to each generation dict (archive/list views)."""
    ids = [g["id"] for g in generations]
    status_map = social_db.publications_for_generations(user_id, ids) if ids else {}
    for g in generations:
        g["publish_status"] = status_map.get(g["id"], {})
    return generations
