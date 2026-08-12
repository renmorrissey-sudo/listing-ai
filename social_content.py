"""Parse the Listing Generator's combined social-post text into per-platform sections.

The Listing Generator prompt (see app.py `build_listing_prompt`) asks Claude for
one blob of text containing three numbered, bracket-tagged posts, e.g.:
    1. [INSTAGRAM] ...
    2. [FACEBOOK] ...
    3. [X/TWITTER] ...

This is a lightweight text parser, not a new AI call. It lets the one-click
Post pipeline select each destination platform's own post out of the single
generation, rather than posting all three platforms' copy verbatim to every
channel. When a platform-specific section can't be found (e.g. LinkedIn,
which the prompt does not generate a dedicated post for), callers fall back
to the Facebook-style post as the closest tone/length match, then to the
full raw social text.
"""

from __future__ import annotations

import re

_SECTION_RE = re.compile(
    r"\[(INSTAGRAM|FACEBOOK|X/TWITTER|X|TWITTER|LINKEDIN|TIKTOK)\]\s*(.*?)(?=\n\s*\d+[.)]\s*\[|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_PLATFORM_ALIASES = {
    "instagram": "instagram",
    "facebook": "facebook",
    "x/twitter": "x",
    "x": "x",
    "twitter": "x",
    "linkedin": "linkedin",
    "tiktok": "tiktok",
}


def parse_platform_sections(social_text: str | None) -> dict:
    """Best-effort split of the combined social text into {platform: caption}."""
    sections: dict[str, str] = {}
    if not social_text:
        return sections
    for match in _SECTION_RE.finditer(social_text):
        tag = match.group(1).strip().lower()
        platform = _PLATFORM_ALIASES.get(tag)
        if not platform:
            continue
        body = re.sub(r"^\d+[.)]\s*", "", match.group(2).strip()).strip()
        if body:
            sections[platform] = body
    return sections


def build_social_content_snapshot(social_text: str | None) -> dict:
    """Normalized social-post model: {baseCaption, <platform>: caption, ...}."""
    sections = parse_platform_sections(social_text)
    return {"baseCaption": (social_text or "").strip(), **sections}


def caption_for_platform(social_content: dict | None, platform: str) -> str:
    """Best available caption for a platform: its own section, else Facebook's, else base."""
    social_content = social_content or {}
    return (
        social_content.get(platform)
        or social_content.get("facebook")
        or social_content.get("baseCaption")
        or ""
    ).strip()
