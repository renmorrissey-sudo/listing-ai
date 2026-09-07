"""Automatic public-record sold-comparable search for CMA reports."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import config


RENTCAST_PROPERTIES_URL = "https://api.rentcast.io/v1/properties"
RENTCAST_AVM_URL = "https://api.rentcast.io/v1/avm/value"
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
    if not isinstance(records, list):
        raise ComparableSearchError("The sold-property records service returned an unexpected response.")

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

def search_market_comparables(payload, *, api_key=None, opener=urllib.request.urlopen):
    """Return RentCast's distance-aware AVM and its most correlated market comps."""
    api_key = (api_key if api_key is not None else config.RENTCAST_API_KEY).strip()
    if not api_key:
        raise ComparableSearchError(
            "Automatic comparable search is not configured yet. Add the RentCast API key to TopAI."
        )

    address = str(payload.get("subject_address") or "").strip()
    city = str(payload.get("city") or "").strip()
    state = str(payload.get("state") or "").strip().upper()
    property_type = PROPERTY_TYPE_MAP.get(str(payload.get("property_type") or "").strip())
    bedrooms = _integer(payload.get("beds"), "bedroom count")
    try:
        bathrooms = float(payload.get("baths"))
    except (TypeError, ValueError) as exc:
        raise ComparableSearchError("Enter a valid bathroom count before building the CMA.") from exc
    square_feet = _integer(payload.get("sqft"), "square footage", 100)
    months = _integer(payload.get("date_range_months"), "comparable lookback period", 1)
    days = _month_days(months)
    requested = _integer(payload.get("comp_count"), "comparable count", 1)
    if not address or not city or len(state) != 2 or not property_type or not days:
        raise ComparableSearchError("Complete the target property and CMA search fields first.")

    params = {
        "address": f"{address}, {city}, {state}",
        "propertyType": property_type,
        "bedrooms": str(bedrooms),
        "bathrooms": str(bathrooms),
        "squareFootage": str(square_feet),
        "maxRadius": "5",
        "daysOld": str(days),
        "compCount": str(max(10, requested)),
        "lookupSubjectAttributes": "true",
    }
    url = f"{RENTCAST_AVM_URL}?{urllib.parse.urlencode(params)}"
    result = _request_json(url, api_key, opener)
    if not isinstance(result, dict):
        raise ComparableSearchError("The property valuation service returned an unexpected response.")

    try:
        valuation = {
            "indicated_value": int(float(result["price"])),
            "range_low": int(float(result["priceRangeLow"])),
            "range_high": int(float(result["priceRangeHigh"])),
            "source": "RentCast automated valuation model",
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ComparableSearchError("RentCast could not calculate a reliable value for this property.") from exc
    if not (1_000 <= valuation["range_low"] <= valuation["indicated_value"] <= valuation["range_high"]):
        raise ComparableSearchError("RentCast returned an invalid valuation range for this property.")

    comparables = []
    for record in result.get("comparables") or []:
        if not isinstance(record, dict):
            continue
        reference_date = _sale_date(record.get("removedDate") or record.get("lastSeenDate"))
        required = (
            record.get("formattedAddress"),
            reference_date,
            record.get("price"),
            record.get("bedrooms"),
            record.get("bathrooms"),
            record.get("squareFootage"),
            record.get("correlation"),
            record.get("distance"),
        )
        if any(value in {None, ""} for value in required):
            continue
        try:
            comparables.append(
                {
                    "address": str(record["formattedAddress"]),
                    "sale_date": reference_date,
                    "sale_price": int(float(record["price"])),
                    "property_type": str(payload.get("property_type")),
                    "beds": int(float(record["bedrooms"])),
                    "baths": float(record["bathrooms"]),
                    "sqft": int(float(record["squareFootage"])),
                    "distance_miles": round(float(record["distance"]), 3),
                    "correlation": float(record["correlation"]),
                    "evidence_type": "avm_listing",
                    "notes": "AVM comparable sale listing; price and reference date are listing-market evidence, not a verified closed-sale record.",
                    "verification_source": "RentCast AVM sale listings",
                    "source_record_id": str(record.get("id") or ""),
                    "assessor_id": "",
                }
            )
        except (TypeError, ValueError):
            continue
    if len(comparables) < requested:
        raise ComparableSearchError(
            f"RentCast found only {len(comparables)} complete, correlated market comparables. "
            "Expand the lookback period or add verified MLS comps manually."
        )
    comparables.sort(key=lambda comp: (-comp["correlation"], comp["distance_miles"]))
    return {"comparables": comparables, "valuation": valuation}
