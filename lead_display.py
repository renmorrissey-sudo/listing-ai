"""User-facing lead presentation helpers. Stored attribution is unchanged."""

from __future__ import annotations

HIDDEN_LEAD_SOURCES = frozenset({
    "ask_topai",
    "external:ask_topai",
})


def _source_text(source) -> str:
    return str(source or "").strip()


def is_hidden_lead_source(source) -> bool:
    raw = _source_text(source).lower()
    if not raw:
        return False
    if raw in HIDDEN_LEAD_SOURCES:
        return True
    return raw.endswith(":ask_topai")


def public_lead_source(source):
    """Return a visible source label, or None when the UI should omit it."""
    if is_hidden_lead_source(source):
        return None
    return _source_text(source) or None


def _lead_get(lead, key, default=None):
    if lead is None:
        return default
    if hasattr(lead, "get"):
        return lead.get(key, default)
    return getattr(lead, key, default)


def show_external_source_badge(lead) -> bool:
    """True for real external ingest sources; false for Ask TopAI-created leads."""
    source = _lead_get(lead, "source")
    if is_hidden_lead_source(source):
        return False
    if _lead_get(lead, "external_source_id"):
        return True
    return _source_text(source).startswith("external:")
