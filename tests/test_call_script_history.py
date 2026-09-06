"""Call Script history, Target Area search, and area-overview lookup."""

from unittest.mock import patch

import call_scripts_db
import db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _message(text):
    block = type("Block", (), {"text": text})()
    return type("Message", (), {"content": [block]})()


def _script_message():
    return _message(
        "---AREA OVERVIEW---\nEstablished residential area. Verify current market data with your MLS and local sources.\n"
        "---OPENING SCRIPT---\nHello, this is Alex.\n"
        "---OBJECTION HANDLERS---\nI understand.\n"
        "---VOICEMAIL SCRIPT---\nPlease call me back."
    )


PAYLOAD = {
    "area": "Westlake Hills, Austin TX",
    "property_type": "Single Family",
    "situation": "General Farming",
    "agent_name": "Alex",
    "key_benefit": "Local market experience",
}


def test_script_generation_is_saved_with_area_overview(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)

    with patch("app.client.messages.create", return_value=_script_message()):
        response = app_client.post("/generate-script", json=PAYLOAD)

    assert response.status_code == 200
    data = response.get_json()
    assert data["generation_id"]
    assert "Established residential area" in data["overview"]
    saved = call_scripts_db.get_by_id(user_id, data["generation_id"])
    assert saved["target_area"] == PAYLOAD["area"]
    assert saved["opening_script"] == "Hello, this is Alex."
    assert "Verify current market data" in saved["area_overview"]


def test_history_search_is_target_area_specific_and_tenant_scoped(app_client, two_users):
    user_id, other_user_id = two_users
    first = call_scripts_db.create_generation(
        user_id,
        inputs={**PAYLOAD, "area": "Westlake Hills, Austin TX"},
        outputs={"overview": "Overview", "opening": "One", "objections": "Two", "voicemail": "Three"},
    )
    call_scripts_db.create_generation(
        user_id,
        inputs={**PAYLOAD, "area": "Cherry Creek, Denver CO"},
        outputs={"overview": "Overview", "opening": "Four", "objections": "Five", "voicemail": "Six"},
    )
    call_scripts_db.create_generation(
        other_user_id,
        inputs={**PAYLOAD, "area": "Westlake Hills, Austin TX"},
        outputs={"overview": "Private", "opening": "Other", "objections": "Other", "voicemail": "Other"},
    )
    _login(app_client, user_id)

    response = app_client.get("/call-scripts?area=westlake%20hills")

    assert response.status_code == 200
    items = response.get_json()["items"]
    assert [item["id"] for item in items] == [first["id"]]
    assert app_client.get(f"/call-scripts/{first['id']}").status_code == 200
    other = call_scripts_db.search_history(other_user_id, area="Westlake")[0]
    assert app_client.get(f"/call-scripts/{other['id']}").status_code == 404


def test_target_area_lookup_returns_real_estate_overview(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    overview = "A practical area overview. Verify current market data with your MLS and local sources."

    with patch("app.client.messages.create", return_value=_message(overview)) as create:
        response = app_client.post("/target-area-overview", json={"area": "Cherry Creek, Denver CO"})

    assert response.status_code == 200
    assert response.get_json()["overview"] == overview
    assert "Cherry Creek, Denver CO" in create.call_args.kwargs["messages"][0]["content"]


def test_call_script_page_has_history_and_area_lookup_controls(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)

    html = app_client.get("/app#coldcall").get_data(as_text=True)

    assert 'id="script-history-btn"' in html
    assert 'id="area-lookup-btn"' in html
    assert 'id="script-history-dialog"' in html
    assert 'data-tab="soverview"' in html
