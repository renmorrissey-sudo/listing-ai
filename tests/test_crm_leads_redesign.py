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
    # Same safe defaults as every other ingestion path (CSV/webhook/manual form).
    assert lead["sms_consent_status"] == "not_certified"
    assert int(lead["sms_sending_blocked"]) == 1


def test_api_create_lead_missing_phone_returns_400(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.post(
        "/api/crm/leads", json={"first_name": "No", "last_name": "Phone"}
    )
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_api_create_lead_duplicate_phone_updates_not_duplicates(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    first = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Dana", "last_name": "One", "phone": "+15551239004"},
    ).get_json()
    second = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Dana", "last_name": "Two", "phone": "+15551239004"},
    ).get_json()
    assert second["lead_id"] == first["lead_id"]
    assert second["duplicate"] is True
    all_leads = crm_filter_leads_helper(u1)
    matches = [l for l in all_leads if l["phone_number"] == "+15551239004"]
    assert len(matches) == 1


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
