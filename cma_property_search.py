"""Automatic public-record sold-comparable search for CMA reports."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import config


RENTCAST_PROPERTIES_URL = "https://api.rentcast.io/v1/properties"
PROPERTY_TYPE_MAP = {
    "single_family": "Single Family",
    "condo": "Condo",
    "townhome": "Townhouse",
    "multi_family": "Multi-Family",
}


class ComparableSearchError(RuntimeError):
    pass


def _integer(value, label, minimum=0):
    try:
        number = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ComparableSearchError(f"Enter a valid {label} before building the CMA.") from exc
    if number < minimum:
        raise ComparableSearchError(f"Enter a valid {label} before building the CMA.")
    return number


def _month_days(months):
    return {1: 31, 3: 92, 6: 184, 12: 366}.get(months)


def _sale_date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10]


def _request_json(url, api_key, opener):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "X-Api-Key": api_key},
        method="GET",
    )
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ComparableSearchError("The sold-property data connection needs a valid RentCast API key.") from exc
        if exc.code == 429:
            raise ComparableSearchError("The sold-property search limit has been reached. Try again later.") from exc
        raise ComparableSearchError("The sold-property records service could not complete this search.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ComparableSearchError("The sold-property records service is temporarily unavailable.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComparableSearchError("The sold-property records service returned an unreadable response.") from exc
    if not isinstance(payload, list):
        raise ComparableSearchError("The sold-property records service returned an unexpected response.")
    return payload


def search_sold_comparables(payload, *, api_key=None, opener=urllib.request.urlopen):
    """Return normalized public-record sales near the subject property."""
    api_key = (api_key if api_key is not None else config.RENTCAST_API_KEY).strip()
    if not api_key:
        raise ComparableSearchError(
            "Automatic sold-property search is not configured yet. Add the RentCast API key to TopAI."
        )

    address = str(payload.get("subject_address") or "").strip()
    city = str(payload.get("city") or "").strip()
    state = str(payload.get("state") or "").strip().upper()
    property_type = PROPERTY_TYPE_MAP.get(str(payload.get("property_type") or "").strip())
    bedrooms = _integer(payload.get("beds"), "bedroom count")
    bathrooms = float(payload.get("baths") or 0)
    square_feet = _integer(payload.get("sqft"), "square footage", 100)
    months = _integer(payload.get("date_range_months"), "closed-sale date range", 1)
    days = _month_days(months)
    if not address or not city or len(state) != 2 or not property_type or not days:
        raise ComparableSearchError("Complete the target property and CMA search fields first.")

    params = {
        "address": f"{address}, {city}, {state}",
        "radius": "5",
        "propertyType": property_type,
        "saleDateRange": str(days),
        "limit": "50",
    }
    url = f"{RENTCAST_PROPERTIES_URL}?{urllib.parse.urlencode(params)}"
    records = _request_json(url, api_key, opener)

    subject_key = " ".join(f"{address} {city} {state}".lower().replace(",", "").split())
    comparables = []
    for record in records:
        if not isinstance(record, dict):
            continue
        formatted_address = str(record.get("formattedAddress") or "").strip()
        address_key = " ".join(formatted_address.lower().replace(",", "").split())
        if not formatted_address or address_key == subject_key:
            continue
        sale_date = _sale_date(record.get("lastSaleDate"))
        sale_price = record.get("lastSalePrice")
        comp_beds = record.get("bedrooms")
        comp_baths = record.get("bathrooms")
        comp_sqft = record.get("squareFootage")
        if not all(value not in {None, ""} for value in (sale_date, sale_price, comp_beds, comp_baths, comp_sqft)):
            continue
        try:
            if date.fromisoformat(sale_date) > date.today():
                continue
            normalized = {
                "address": formatted_address,
                "sale_date": sale_date,
                "sale_price": int(float(sale_price)),
                "property_type": str(payload.get("property_type")),
                "beds": int(float(comp_beds)),
                "baths": float(comp_baths),
                "sqft": int(float(comp_sqft)),
                "distance_miles": record.get("distance"),
                "notes": "Public-record sale" + (f" · APN {record.get('assessorID')}" if record.get("assessorID") else ""),
                "verification_source": "RentCast public property records",
                "source_record_id": str(record.get("id") or ""),
                "assessor_id": str(record.get("assessorID") or ""),
            }
        except (TypeError, ValueError):
            continue
        if normalized["sale_price"] < 1000 or normalized["sqft"] < 100:
            continue
        # Keep a broad candidate pool; the CMA scorer performs final similarity ranking.
        size_ratio = normalized["sqft"] / square_feet
        if 0.45 <= size_ratio <= 1.75 and abs(normalized["beds"] - bedrooms) <= 3 and abs(normalized["baths"] - bathrooms) <= 3:
            comparables.append(normalized)

    requested = _integer(payload.get("comp_count"), "comparable count", 1)
    if len(comparables) < requested:
        raise ComparableSearchError(
            f"Only {len(comparables)} complete public-record sales were found within 5 miles and the selected date range. "
            "Expand the date range or add verified MLS comps manually."
        )
    return comparables
