"""Subscriber target-area research page."""


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def test_research_requires_authentication(app_client):
    response = app_client.get("/research", follow_redirects=False)
    assert response.status_code in (301, 302)
    assert "/app" in response.headers["Location"]


def test_research_page_has_targeted_sources(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)

    html = app_client.get(
        "/research?area=Meadow%20Ranch%2C%20Littleton%2C%20CO"
    ).get_data(as_text=True)

    assert "Research a Target Area" in html
    assert 'value="Meadow Ranch, Littleton, CO"' in html
    assert "Zillow Recently Sold" in html
    assert "Redfin Area & Market Trends" in html
    assert "Redfin Data Center" in html
    assert "Realtor.com Research Data" in html
    assert "Median &amp; Average DOM" in html
    assert "Months of Inventory" in html
    assert "Housing Market News" in html
    assert "County Assessor & Sales Records" in html
    assert "FEMA Flood Maps" in html
    assert "Use your MLS as the final authority" in html


def test_research_zillow_builder_anchors_city_before_neighborhood(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    html = app_client.get("/research").get_data(as_text=True)

    assert "usersSearchTerm: location.cityState" in html
    assert "filterState: { keywords: { value: location.neighborhood } }" in html
    assert "www.zillow.com/${citySlug(location)}/sold/" in html
    assert "Meadow Ranch, Littleton, CO" in html
    assert "target = '_blank'" in html
    assert "noopener noreferrer" in html
