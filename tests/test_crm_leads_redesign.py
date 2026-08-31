"""CRM Leads Page Redesign: header CTAs, New Lead drawer JSON API, duplicate-check
endpoint, search filter, empty state, and Lead Sources redesign.

Business logic (ingest_external_lead, consent defaults, tenant isolation) is
unchanged — these tests confirm the new thin UI/API surface reuses it correctly.
"""

import db
import external_leads_db as xdb
from external_leads.ingest import ingest_external_lead
from sms_authorization import record_one_to_one_attestation


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def test_leads_page_has_three_header_ctas(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "+ New Lead" in html
    assert "Import CSV" in html
    assert "Connect Lead Sources" in html
    assert '/crm/external-leads/import' in html
    assert '/crm/external-sources' in html


def test_leads_page_empty_state_when_no_leads(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "No leads yet" in html
    assert "Create your first lead or import an existing lead list." in html


def test_leads_page_filtered_empty_state_differs_from_true_empty(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    ingest_external_lead(
        u1, {"full_name": "Ann Agent", "phone": "+15551239001"}, method="manual"
    )
    html = app_client.get("/crm/leads?status=closed_lost").get_data(as_text=True)
    assert "No leads match this filter" in html
    assert "No leads yet" not in html


def test_api_create_lead_requires_auth(app_client):
    res = app_client.post("/api/crm/leads", json={"first_name": "A", "last_name": "B", "phone": "+15551239002"})
    assert res.status_code == 401


def test_api_create_lead_creates_via_shared_ingest(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.post(
        "/api/crm/leads",
        json={
            "first_name": "Casey",
            "last_name": "Buyer",
            "phone": "(555) 123-9003",
            "email": "casey@example.com",
            "status": "new",
            "notes": "Met at open house",
        },
    )
    assert res.status_code == 201
    data = res.get_json()
    assert data["ok"] is True
    assert data["created"] is True
    assert data["redirect_url"] == f"/crm/leads/{data['lead_id']}"

    lead = db.get_lead(data["lead_id"], u1)
    assert lead is not None
    assert lead["name"] == "Casey Buyer"
    assert lead["phone_number"] == "+15551239003"
    assert lead["email"] == "casey@example.com"
    # Same safe defaults as every other ingestion path (CSV/webhook/manual form).
    assert lead["sms_consent_status"] == "not_certified"
    assert int(lead["sms_sending_blocked"]) == 1


def test_api_update_lead_contact_email(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_business_profile(
        u1,
        agent_name="Ada Agent",
        phone_number="(303) 555-0199",
        brokerage_name="Ada Realty",
    )
    created = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Email", "last_name": "Capture", "phone": "+15551239013"},
    ).get_json()
    lead_id = created["lead_id"]

    bad = app_client.post(
        f"/api/crm/leads/{lead_id}/contact",
        json={"email": "not-an-email"},
    )
    assert bad.status_code == 400

    res = app_client.post(
        f"/api/crm/leads/{lead_id}/contact",
        json={
            "name": "Email Capture Updated",
            "phone_number": "(555) 123-9014",
            "email": "capture@example.com",
            "lead_type": "buyer",
            "property_interest": "Condo near downtown",
            "notes": "Prefers afternoon calls",
            "next_action": "Send listings",
        },
    )
    assert res.status_code == 200
    updated = res.get_json()["lead"]
    assert updated["name"] == "Email Capture Updated"
    assert updated["phone_number"] == "+15551239014"
    assert updated["email"] == "capture@example.com"
    assert updated["lead_type"] == "buyer"
    assert updated["property_interest"] == "Condo near downtown"
    assert updated["notes"] == "Prefers afternoon calls"
    assert updated["next_action"] == "Send listings"

    detail = app_client.get(f"/api/crm/leads/{lead_id}").get_json()
    assert detail["lead"]["email"] == "capture@example.com"
    listed = app_client.get("/api/crm/leads").get_json()["leads"]
    assert any(
        item["id"] == lead_id and item["email"] == "capture@example.com"
        for item in listed
    )
    html = app_client.get(f"/crm/leads/{lead_id}").get_data(as_text=True)
    assert "capture@example.com" in html
    assert 'href="mailto:capture%40example.com"' in html
    assert "Competitive%20market%20analysis%20for%20Condo%20near%20downtown" in html
    assert (
        "Warm%20regards%2C%0AAda%20Agent%0A%28303%29%20555-0199%0AAda%20Realty"
        in html
    )

    leads_html = app_client.get("/crm/leads?active=1").get_data(as_text=True)
    assert 'href="mailto:capture%40example.com?subject=Following%20up%20from%20TopAI%20Real%20Estate%20Tools' in leads_html
    assert (
        "Warm%20regards%2C%0AAda%20Agent%0A%28303%29%20555-0199%0AAda%20Realty"
        in leads_html
    )


def test_api_update_lead_contact_rejects_duplicate_phone(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    first = app_client.post(
        "/api/crm/leads",
        json={"first_name": "First", "last_name": "Lead", "phone": "+15551239015"},
    ).get_json()
    second = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Second", "last_name": "Lead", "phone": "+15551239016"},
    ).get_json()

    res = app_client.post(
        f"/api/crm/leads/{second['lead_id']}/contact",
        json={"phone_number": "(555) 123-9015"},
    )

    assert res.status_code == 409
    assert db.get_lead(second["lead_id"], u1)["phone_number"] == "+15551239016"
    assert "First Lead" in res.get_json()["error"]


def test_api_create_lead_missing_phone_returns_400(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.post(
        "/api/crm/leads", json={"first_name": "No", "last_name": "Phone"}
    )
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_api_create_lead_duplicate_phone_does_not_overwrite(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    first = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Dana", "last_name": "One", "phone": "+15551239004"},
    )
    first_json = first.get_json()
    assert first.status_code == 201
    second = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Dana", "last_name": "Two", "phone": "+15551239004"},
    )
    second_json = second.get_json()
    assert second.status_code == 409
    assert second_json["duplicate"] is True
    assert second_json["lead_id"] == first_json["lead_id"]
    assert "Dana One" in second_json["error"]
    lead = db.get_lead(first_json["lead_id"], u1)
    assert lead["name"] == "Dana One"
    all_leads = crm_filter_leads_helper(u1)
    matches = [l for l in all_leads if l["phone_number"] == "+15551239004"]
    assert len(matches) == 1
    assert matches[0]["name"] == "Dana One"


def crm_filter_leads_helper(user_id):
    import crm_db

    return crm_db.filter_leads(user_id, limit=1000)


def test_api_check_duplicate_scoped_to_tenant(app_client, two_users):
    u1, u2 = two_users
    _login(app_client, u1)
    created = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Eve", "last_name": "Owner", "phone": "+15551239005"},
    ).get_json()

    res = app_client.get("/api/crm/leads/check-duplicate?phone=5551239005")
    data = res.get_json()
    assert data["duplicate"] is True
    assert data["lead_id"] == created["lead_id"]
    assert data["url"] == f"/crm/leads/{created['lead_id']}"

    # No cross-tenant leakage: same phone under a different tenant is not a duplicate.
    _login(app_client, u2)
    res2 = app_client.get("/api/crm/leads/check-duplicate?phone=5551239005")
    assert res2.get_json()["duplicate"] is False


def test_api_check_duplicate_no_match(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.get("/api/crm/leads/check-duplicate?phone=5559999999")
    assert res.get_json() == {"duplicate": False}


def test_leads_search_filters_by_name_phone_email(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    ingest_external_lead(
        u1, {"full_name": "Zara Zephyr", "phone": "+15551239006", "email": "zara@example.com"},
        method="manual",
    )
    ingest_external_lead(
        u1, {"full_name": "Unrelated Person", "phone": "+15551239007"}, method="manual"
    )
    html = app_client.get("/crm/leads?q=Zara").get_data(as_text=True)
    assert "Zara Zephyr" in html
    assert "Unrelated Person" not in html

    html_phone = app_client.get("/crm/leads?q=1239006").get_data(as_text=True)
    assert "Zara Zephyr" in html_phone


def test_leads_search_is_tenant_scoped(app_client, two_users):
    u1, u2 = two_users
    ingest_external_lead(
        u1, {"full_name": "SharedName Tester", "phone": "+15551239008"}, method="manual"
    )
    _login(app_client, u2)
    html = app_client.get("/crm/leads?q=SharedName").get_data(as_text=True)
    assert "SharedName Tester" not in html


def test_lead_sources_page_shows_connected_available_and_coming_soon(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    xdb.create_external_lead_source(
        u1, name="My Webhook Source", category="webhook", provider_key="my-src",
        import_method="webhook",
    )
    html = app_client.get("/crm/external-sources").get_data(as_text=True)
    assert "Connected" in html
    assert "My Webhook Source" in html
    assert "Available now" in html
    assert "CSV Upload" in html
    assert "Webhook / API" in html
    assert "Popular lead providers" in html
    assert "Zillow Premier Agent" in html
    assert "Coming soon" in html


def test_lead_source_manage_page_and_secret_rotation(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    source_id = xdb.create_external_lead_source(
        u1, name="Rotatable Source", category="webhook", provider_key="rotatable",
        import_method="webhook", webhook_secret_hash="oldhash",
    )
    res = app_client.get(f"/crm/external-sources/{source_id}")
    assert res.status_code == 200
    assert b"Rotatable Source" in res.data

    rotate = app_client.post(
        f"/crm/external-sources/{source_id}", data={"action": "rotate_secret"}
    )
    assert rotate.status_code == 200
    assert b"New webhook secret" in rotate.data


def test_lead_detail_shows_certified_consent_not_unverified(app_client, two_users):
    """A certified lead must never be mislabeled Unverified or warned as unsendable."""
    u1, _ = two_users
    _login(app_client, u1)
    result = ingest_external_lead(
        u1, {"full_name": "Certified Carla", "phone": "+15551239009"}, method="manual"
    )
    lead_id = result["lead_id"]
    _att_id, err = record_one_to_one_attestation(
        u1, lead_id, message_body="Hi Carla, following up", source_page="test"
    )
    assert err is None
    lead = db.get_lead(lead_id, u1)
    assert lead["sms_consent_status"] == "user_certified"
    assert int(lead["sms_sending_blocked"]) == 0

    html = app_client.get(f"/crm/leads/{lead_id}").get_data(as_text=True)
    assert "Certified" in html
    assert "Unverified" not in html
    assert "SMS cannot be sent until consent is verified" not in html

    list_html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "Certified Carla" in list_html


def test_lead_source_manage_page_enforces_tenant_isolation(app_client, two_users):
    u1, u2 = two_users
    source_id = xdb.create_external_lead_source(
        u1, name="Owner Only Source", category="other", provider_key="owner-only",
        import_method="manual",
    )
    _login(app_client, u2)
    res = app_client.get(f"/crm/external-sources/{source_id}", follow_redirects=True)
    assert b"Owner Only Source" not in res.data
