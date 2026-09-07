"""Validation, comparable selection, and pricing calculations for CMA reports.

The CMA workflow deliberately works from agent-supplied closed-sale facts.  It
does not ask an LLM to invent property records or scrape consumer portals.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median


ALLOWED_COMP_COUNTS = frozenset(range(3, 9))
ALLOWED_DATE_RANGES = frozenset({1, 3, 6, 12})
PROPERTY_TYPES = {
    "single_family": "Single-family home",
    "condo": "Condo",
    "townhome": "Townhome",
    "multi_family": "Multi-family",
}
PROSPECT_TYPES = {"seller": "Seller", "buyer": "Buyer"}
US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC".split()
)


class CMAValidationError(ValueError):
    pass


def _text(value, label, *, required=True, max_length=200):
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not cleaned:
        raise CMAValidationError(f"{label} is required.")
    if len(cleaned) > max_length:
        raise CMAValidationError(f"{label} must be {max_length} characters or fewer.")
    return cleaned


def _decimal(value, label, *, minimum, maximum, required=True):
    raw = str(value if value is not None else "").replace("$", "").replace(",", "").strip()
    if not raw and not required:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise CMAValidationError(f"Enter a valid {label}.") from exc
    if not number.is_finite() or number < Decimal(str(minimum)) or number > Decimal(str(maximum)):
        raise CMAValidationError(f"{label} must be between {minimum} and {maximum}.")
    return number


def _integer(value, label, *, minimum, maximum):
    number = _decimal(value, label, minimum=minimum, maximum=maximum)
    if number != number.to_integral_value():
        raise CMAValidationError(f"{label} must be a whole number.")
    return int(number)


def _money_int(value, label):
    number = _decimal(value, label, minimum=1_000, maximum=100_000_000)
    return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _subtract_months(day, months):
    month_index = day.year * 12 + day.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _parse_date(value, label):
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise CMAValidationError(f"Enter a valid {label}.") from exc


def _clean_subject(payload):
    prospect_type = str(payload.get("prospect_type") or "").strip().lower()
    if prospect_type not in PROSPECT_TYPES:
        raise CMAValidationError("Choose whether this CMA is for a seller or buyer.")
    property_type = str(payload.get("property_type") or "").strip().lower()
    if property_type not in PROPERTY_TYPES:
        raise CMAValidationError("Choose a valid property type.")
    state = _text(payload.get("state"), "State", max_length=100).upper()
    if state not in US_STATE_CODES:
        raise CMAValidationError("Enter a valid two-letter U.S. state code.")
    return {
        "prospect_type": prospect_type,
        "prospect_type_label": PROSPECT_TYPES[prospect_type],
        "prospect_name": _text(
            payload.get("prospect_name"), "Prospect name", required=False, max_length=120
        ),
        "subject_address": _text(payload.get("subject_address"), "Target address or area"),
        "city": _text(payload.get("city"), "City", max_length=100),
        "state": state,
        "property_type": property_type,
        "property_type_label": PROPERTY_TYPES[property_type],
        "beds": _integer(payload.get("beds"), "Bedrooms", minimum=0, maximum=20),
        "baths": float(_decimal(payload.get("baths"), "Bathrooms", minimum=0, maximum=20)),
        "sqft": _integer(payload.get("sqft"), "Square footage", minimum=100, maximum=100_000),
    }


def _clean_comparable(raw, index):
    label = f"Comparable {index}"
    property_type = str(raw.get("property_type") or "").strip().lower()
    if property_type not in PROPERTY_TYPES:
        raise CMAValidationError(f"Choose a valid property type for {label.lower()}.")
    return {
        "address": _text(raw.get("address"), f"{label} address"),
        "sale_date": _parse_date(raw.get("sale_date"), f"sale date for {label.lower()}"),
        "sale_price": _money_int(raw.get("sale_price"), f"sale price for {label.lower()}"),
        "property_type": property_type,
        "property_type_label": PROPERTY_TYPES[property_type],
        "beds": _integer(raw.get("beds"), f"bedrooms for {label.lower()}", minimum=0, maximum=20),
        "baths": float(
            _decimal(raw.get("baths"), f"bathrooms for {label.lower()}", minimum=0, maximum=20)
        ),
        "sqft": _integer(
            raw.get("sqft"), f"square footage for {label.lower()}", minimum=100, maximum=100_000
        ),
        "distance_miles": (
            float(
                _decimal(
                    raw.get("distance_miles"),
                    f"distance for {label.lower()}",
                    minimum=0,
                    maximum=500,
                    required=False,
                )
            )
            if str(raw.get("distance_miles") or "").strip()
            else None
        ),
        "notes": _text(raw.get("notes"), f"Notes for {label.lower()}", required=False, max_length=500),
    }


def _score(subject, comp, today):
    score = Decimal("100")
    reasons = []
    if comp["property_type"] == subject["property_type"]:
        reasons.append("same property type")
    else:
        score -= Decimal("28")
        reasons.append("different property type")

    sqft_delta = abs(comp["sqft"] - subject["sqft"]) / subject["sqft"]
    score -= Decimal(str(min(35, sqft_delta * 45)))
    if sqft_delta <= 0.15:
        reasons.append("similar size")

    bed_delta = abs(comp["beds"] - subject["beds"])
    bath_delta = abs(comp["baths"] - subject["baths"])
    score -= Decimal(str(min(18, bed_delta * 6)))
    score -= Decimal(str(min(15, bath_delta * 5)))
    if bed_delta == 0 and bath_delta <= 0.5:
        reasons.append("similar bed/bath count")

    if comp["distance_miles"] is not None:
        score -= Decimal(str(min(20, comp["distance_miles"] * 3)))
        if comp["distance_miles"] <= 1:
            reasons.append("within 1 mile")

    age_days = max(0, (today - comp["sale_date"]).days)
    score -= Decimal(str(min(10, age_days / 365 * 5)))
    if age_days <= 90:
        reasons.append("recent sale")

    return max(Decimal("0"), score).quantize(Decimal("0.1")), reasons


def _round_currency(value):
    return int((Decimal(str(value)) / Decimal("1000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 1000)


def build_cma(payload, *, today=None):
    """Return validated criteria, ranked comps, exclusions, and report statistics."""
    if not isinstance(payload, dict):
        raise CMAValidationError("Submit valid CMA details.")
    today = today or date.today()
    subject = _clean_subject(payload)
    try:
        comp_count = int(payload.get("comp_count"))
        date_range_months = int(payload.get("date_range_months"))
    except (TypeError, ValueError) as exc:
        raise CMAValidationError("Choose a valid comparable count and date range.") from exc
    if comp_count not in ALLOWED_COMP_COUNTS:
        raise CMAValidationError("Comparable count must be between 3 and 8.")
    if date_range_months not in ALLOWED_DATE_RANGES:
        raise CMAValidationError("Date range must be 1, 3, 6, or 12 months.")

    raw_comps = payload.get("comparables")
    if not isinstance(raw_comps, list):
        raise CMAValidationError("Add candidate comparable sales.")
    if len(raw_comps) > 50:
        raise CMAValidationError("A CMA can include at most 50 candidate sales.")
    cleaned = [_clean_comparable(raw, i) for i, raw in enumerate(raw_comps, 1) if isinstance(raw, dict)]
    cutoff = _subtract_months(today, date_range_months)
    eligible = []
    excluded = []
    for comp in cleaned:
        if comp["sale_date"] > today:
            excluded.append({**comp, "exclusion_reason": "Sale date is in the future"})
            continue
        if comp["sale_date"] < cutoff:
            excluded.append({**comp, "exclusion_reason": "Outside selected date range"})
            continue
        similarity, reasons = _score(subject, comp, today)
        eligible.append({**comp, "similarity_score": float(similarity), "match_reasons": reasons})

    if len(eligible) < comp_count:
        raise CMAValidationError(
            f"Add at least {comp_count} candidate sales dated between {cutoff.isoformat()} "
            f"and {today.isoformat()}. Only {len(eligible)} currently qualify."
        )

    eligible.sort(key=lambda comp: (-comp["similarity_score"], -comp["sale_date"].toordinal(), comp["address"]))
    selected = eligible[:comp_count]
    for rank, comp in enumerate(selected, 1):
        comp["rank"] = rank
        comp["price_per_sqft"] = round(comp["sale_price"] / comp["sqft"], 2)
        comp["size_delta_pct"] = round((comp["sqft"] - subject["sqft"]) / subject["sqft"] * 100, 1)
        comp["subject_size_indication"] = _round_currency(comp["price_per_sqft"] * subject["sqft"])

    prices = [comp["sale_price"] for comp in selected]
    ppsf_values = [comp["price_per_sqft"] for comp in selected]
    weights = [max(1, comp["similarity_score"]) for comp in selected]
    weighted_ppsf = sum(value * weight for value, weight in zip(ppsf_values, weights)) / sum(weights)
    indications = [comp["subject_size_indication"] for comp in selected]
    indicated_value = _round_currency(weighted_ppsf * subject["sqft"])
    spread = (max(indications) - min(indications)) / indicated_value if indicated_value else 1
    average_score = sum(comp["similarity_score"] for comp in selected) / len(selected)
    confidence = "High" if average_score >= 85 and spread <= 0.2 else "Moderate" if average_score >= 70 else "Limited"

    analysis = {
        "selected_count": len(selected),
        "candidate_count": len(cleaned),
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "average_sale_price": _round_currency(sum(prices) / len(prices)),
        "median_sale_price": _round_currency(median(prices)),
        "lowest_sale_price": min(prices),
        "highest_sale_price": max(prices),
        "average_price_per_sqft": round(sum(ppsf_values) / len(ppsf_values), 2),
        "indicated_value": indicated_value,
        "indicated_range_low": min(indications),
        "indicated_range_high": max(indications),
        "average_similarity_score": round(average_score, 1),
        "confidence": confidence,
    }
    criteria = {
        **subject,
        "comp_count": comp_count,
        "date_range_months": date_range_months,
        "date_cutoff": cutoff.isoformat(),
        "report_date": today.isoformat(),
    }

    def serialize(comp):
        return {**comp, "sale_date": comp["sale_date"].isoformat()}

    return {
        "criteria": criteria,
        "selected_comparables": [serialize(comp) for comp in selected],
        "excluded_comparables": [serialize(comp) for comp in excluded],
        "analysis": analysis,
    }
