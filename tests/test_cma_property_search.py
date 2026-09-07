import json
from datetime import date, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import cma_db
import cma_property_search


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _search_payload():
    return {
        "prospect_type": "seller",
        "subject_address": "8533 S Miller Court",
        "city": "Littleton",
        "state": "CO",
        "property_type": "single_family",
        "beds": 5,
        "baths": 5,
        "sqft": 4770,
        "comp_count": 3,
        "date_range_months": 6,
    }


def _records():
    sold = (date.today() - timedelta(days=45)).isoformat() + "T00:00:00.000Z"
    return [
        {
            "id": f"record-{index}",
            "formattedAddress": f"{8500 + index} Comparable Way, Littleton, CO 80127",
            "propertyType": "Single Family",
            "bedrooms": 4 + (index % 2),
            "bathrooms": 4,
            "squareFootage": 4300 + index * 100,
            "lastSaleDate": sold,
            "lastSalePrice": 900000 + index * 25000,
            "assessorID": f"APN-{index}",
        }
        for index in range(4)
    ]


def test_rentcast_search_requests_local_recorded_sales_and_maps_provenance():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(_records())

    comps = cma_property_search.search_sold_comparables(
        _search_payload(), api_key="test-key", opener=opener
    )

    query = parse_qs(urlparse(captured["request"].full_url).query)
    assert query["address"] == ["8533 S Miller Court, Littleton, CO"]
    assert query["radius"] == ["5"]
    assert query["propertyType"] == ["Single Family"]
    assert query["saleDateRange"] == ["184"]
    assert captured["request"].headers["X-api-key"] == "test-key"
    assert len(comps) == 4
    assert comps[0]["verification_source"] == "RentCast public property records"
    assert comps[0]["assessor_id"] == "APN-0"


def test_automatic_cma_requires_explicit_records_provider_disclosure(app_client, two_users):
    user_id, _ = two_users
    with app_client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 1
    payload = _search_payload()
    payload["automatic_search"] = True

    response = app_client.post("/api/cma/reports", json=payload)

    assert response.status_code == 400
    assert "Acknowledge" in response.get_json()["error"]


def test_automatic_cma_searches_builds_and_saves_report(app_client, two_users):
    user_id, _ = two_users
    with app_client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 1
    payload = _search_payload()
    payload.update({"automatic_search": True, "records_provider_disclosure_accepted": True})

    with patch("cma_routes.search_sold_comparables", return_value=[
        {
            "address": row["formattedAddress"],
            "sale_date": row["lastSaleDate"][:10],
            "sale_price": row["lastSalePrice"],
            "property_type": "single_family",
            "beds": row["bedrooms"],
            "baths": row["bathrooms"],
            "sqft": row["squareFootage"],
            "distance_miles": None,
            "notes": "Public-record sale",
            "verification_source": "RentCast public property records",
            "source_record_id": row["id"],
            "assessor_id": row["assessorID"],
        }
        for row in _records()
    ]):
        response = app_client.post("/api/cma/reports", json=payload)

    assert response.status_code == 201
    saved = cma_db.get_report(user_id, response.get_json()["report_id"])
    assert len(saved["selected_comparables"]) == 3
    assert saved["selected_comparables"][0]["verification_source"] == "RentCast public property records"
    assert saved["analysis"]["public_record_count"] == 4


def test_builder_has_prominent_automatic_build_action(app_client, two_users):
    user_id, _ = two_users
    with app_client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 1
    html = app_client.get("/cma").get_data(as_text=True)
    assert 'id="build-cma-primary"' in html
    assert ">Build CMA</button>" in html
    assert 'id="records-provider-disclosure"' in html
    assert "sent to RentCast" in html
    assert "automatic_search: true" in html
