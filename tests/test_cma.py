"""Competitive Market Analysis builder, selection, persistence, and tenancy."""

from datetime import date, timedelta

import cma_db
import db
from cma_service import CMAValidationError, build_cma


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _payload(*, today=None, comp_count=3, months=6):
    today = today or date.today()
    comps = [
        {
            "address": "101 Close Match St",
            "sale_date": (today - timedelta(days=20)).isoformat(),
            "sale_price": "500000",
            "property_type": "single_family",
            "beds": "3",
            "baths": "2",
            "sqft": "2000",
            "distance_miles": "0.4",
            "notes": "Similar condition",
        },
        {
            "address": "202 Similar Ave",
            "sale_date": (today - timedelta(days=45)).isoformat(),
            "sale_price": "525000",
            "property_type": "single_family",
            "beds": "3",
            "baths": "2.5",
            "sqft": "2100",
            "distance_miles": "0.8",
            "notes": "Updated kitchen",
        },
        {
            "address": "303 Nearby Road",
            "sale_date": (today - timedelta(days=70)).isoformat(),
            "sale_price": "475000",
            "property_type": "single_family",
            "beds": "3",
            "baths": "2",
            "sqft": "1900",
            "distance_miles": "1.1",
            "notes": "",
        },
        {
            "address": "404 Weak Match Lane",
            "sale_date": (today - timedelta(days=30)).isoformat(),
            "sale_price": "700000",
            "property_type": "condo",
            "beds": "1",
            "baths": "1",
            "sqft": "900",
            "distance_miles": "8",
            "notes": "Different property type",
        },
    ]
    return {
        "prospect_type": "seller",
        "prospect_name": "Jordan Seller",
        "subject_address": "100 Subject Street",
        "city": "Littleton",
        "state": "co",
        "property_type": "single_family",
        "beds": "3",
        "baths": "2",
        "sqft": "2000",
        "comp_count": comp_count,
        "date_range_months": months,
        "comparables": comps,
    }


def test_build_cma_selects_requested_best_matches_and_calculates_summary():
    report = build_cma(_payload(today=date(2026, 9, 7)), today=date(2026, 9, 7))

    assert report["criteria"]["state"] == "CO"
    assert report["criteria"]["comp_count"] == 3
    assert [item["address"] for item in report["selected_comparables"]] == [
        "101 Close Match St",
        "303 Nearby Road",
        "202 Similar Ave",
    ]
    assert report["analysis"]["selected_count"] == 3
    assert report["analysis"]["candidate_count"] == 4
    assert report["analysis"]["indicated_value"] == 500000
    assert report["analysis"]["average_price_per_sqft"] == 250.0


def test_build_cma_excludes_sales_outside_date_range():
    payload = _payload(today=date(2026, 9, 7))
    payload["comparables"][2]["sale_date"] = "2025-01-01"
    payload["comparables"][3]["sale_date"] = "2025-01-01"

    try:
        build_cma(payload, today=date(2026, 9, 7))
    except CMAValidationError as exc:
        assert "Only 2 currently qualify" in str(exc)
        assert "2026-03-07" in str(exc)
    else:
        raise AssertionError("Expected an insufficient eligible comparables error")


def test_build_cma_enforces_supported_dropdown_values():
    payload = _payload(today=date(2026, 9, 7))
    payload["comp_count"] = 9
    try:
        build_cma(payload, today=date(2026, 9, 7))
    except CMAValidationError as exc:
        assert "between 3 and 8" in str(exc)
    else:
        raise AssertionError("Expected invalid comp count to fail")

    payload = _payload(today=date(2026, 9, 7))
    payload["date_range_months"] = 2
    try:
        build_cma(payload, today=date(2026, 9, 7))
    except CMAValidationError as exc:
        assert "1, 3, 6, or 12" in str(exc)
    else:
        raise AssertionError("Expected invalid date range to fail")


def test_cma_builder_requires_authentication(app_client):
    response = app_client.get("/cma", follow_redirects=False)
    assert response.status_code in (301, 302)
    assert "/app" in response.headers["Location"]


def test_cma_builder_has_required_workflow_controls(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    html = app_client.get("/cma").get_data(as_text=True)

    assert "Create a Competitive Market Analysis" in html
    assert 'id="city"' in html and 'id="state"' in html
    for count in range(3, 9):
        assert f'value="{count}">{count} houses' in html
    assert "Past 1 month" in html
    assert "Past 3 months" in html
    assert "Past 6 months" in html
    assert "Past 12 months" in html
    assert "candidate closed sales" in html
    assert 'href="/cma" class="active"' in html


def test_create_and_render_cma_report(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)

    response = app_client.post("/api/cma/reports", json=_payload())
    assert response.status_code == 201
    body = response.get_json()
    assert body["report_id"]
    assert body["detail_url"].endswith(f"/cma/reports/{body['report_id']}")

    saved = cma_db.get_report(user_id, body["report_id"])
    assert saved["subject_address"] == "100 Subject Street"
    assert len(saved["selected_comparables"]) == 3

    html = app_client.get(body["detail_url"]).get_data(as_text=True)
    assert "Competitive Market Analysis" in html
    assert "100 Subject Street" in html
    assert "Jordan Seller" in html
    assert "101 Close Match St" in html
    assert "Print / Save PDF" in html
    assert "not an appraisal" in html


def test_cma_report_is_tenant_scoped(app_client, two_users):
    owner_id, other_id = two_users
    report = build_cma(_payload())
    saved = cma_db.create_report(owner_id, report)

    _login(app_client, other_id)
    response = app_client.get(f"/cma/reports/{saved['id']}")
    assert response.status_code == 404
    assert "does not exist or belongs to another account" in response.get_data(as_text=True)


def test_cma_api_rejects_bad_state_without_saving(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    before = len(cma_db.list_recent(user_id, limit=50))
    payload = _payload()
    payload["state"] = "Colorado"

    response = app_client.post("/api/cma/reports", json=payload)

    assert response.status_code == 400
    assert "two-letter" in response.get_json()["error"]
    assert len(cma_db.list_recent(user_id, limit=50)) == before


def test_cma_generation_records_tool_usage(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    response = app_client.post("/api/cma/reports", json=_payload())
    assert response.status_code == 201
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM tool_usage WHERE user_id = ? AND tool_key = ?",
            (user_id, "cma_generator"),
        ).fetchone()
    assert row["count"] >= 1
