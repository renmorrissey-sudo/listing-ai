"""Pragmatic street-address normalization for Listing Generator search/versioning.

This is a text-normalization pass, not geocoding or address validation: it
standardizes case, punctuation, and common street-suffix/directional
abbreviations so trivial formatting differences ("Dr" vs "Drive") match to
the same key. It intentionally does NOT call any external geocoding API —
none exists in this app — and never performs fuzzy/distance matching, so it
will not merge two genuinely different properties.

Only the primary address line (the text before the first comma) is used for
the matching key. City/state/ZIP that follows a comma is ignored for
matching purposes but the original, full, user-entered address is always
preserved separately for display (see listing_generations_db.create_generation,
which stores both `display_address` and `normalized_address`).
"""

from __future__ import annotations

import re

# Common USPS street-suffix abbreviations -> full word. Not exhaustive; covers
# the suffixes an agent is realistically going to type for a residential listing.
_SUFFIX_MAP = {
    "ave": "avenue",
    "aven": "avenue",
    "avn": "avenue",
    "avnue": "avenue",
    "blvd": "boulevard",
    "boul": "boulevard",
    "boulv": "boulevard",
    "cir": "circle",
    "circ": "circle",
    "ct": "court",
    "cv": "cove",
    "cres": "crescent",
    "xing": "crossing",
    "dr": "drive",
    "drv": "drive",
    "expy": "expressway",
    "exp": "expressway",
    "expr": "expressway",
    "grn": "green",
    "hwy": "highway",
    "highwy": "highway",
    "hl": "hill",
    "hls": "hills",
    "is": "island",
    "jct": "junction",
    "lk": "lake",
    "ln": "lane",
    "mnr": "manor",
    "pkwy": "parkway",
    "pky": "parkway",
    "pl": "place",
    "plz": "plaza",
    "pt": "point",
    "rd": "road",
    "rdg": "ridge",
    "rte": "route",
    "sq": "square",
    "sta": "station",
    "st": "street",
    "str": "street",
    "ter": "terrace",
    "terr": "terrace",
    "trl": "trail",
    "tr": "trail",
    "vly": "valley",
    "vlg": "village",
    "vw": "view",
}

_DIRECTION_MAP = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "ne": "northeast",
    "nw": "northwest",
    "se": "southeast",
    "sw": "southwest",
}

_STRIP_PUNCTUATION_RE = re.compile(r"[^\w\s#]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_address_key(raw: str | None) -> str:
    """Return a stable, tenant-search-friendly key for a street address.

    Examples that normalize to the SAME key:
        "12015 Wandsworth Dr"
        "12015 Wandsworth Drive"
        "12015 Wandsworth Drive, Tampa FL"   (city/state after the comma is ignored)

    Examples that do NOT collide (different properties/units):
        "12015 Wandsworth Drive" vs "12017 Wandsworth Drive"
        "12015 Wandsworth Drive" vs "12015 Wandsworth Drive Apt 4B"
    """
    if not raw:
        return ""
    primary_line = raw.split(",", 1)[0]
    cleaned = _STRIP_PUNCTUATION_RE.sub(" ", primary_line.lower())
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return ""
    tokens = []
    for tok in cleaned.split(" "):
        if tok in _SUFFIX_MAP:
            tokens.append(_SUFFIX_MAP[tok])
        elif tok in _DIRECTION_MAP:
            tokens.append(_DIRECTION_MAP[tok])
        else:
            tokens.append(tok)
    return " ".join(tokens)
