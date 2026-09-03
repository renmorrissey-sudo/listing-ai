import logging

import db
from voice_provider import (
    VAPI_VARIABLE_KEYS,
    VapiVoiceProvider,
    build_live_voice_assistant_overrides,
    build_vapi_variable_values,
    log_variable_values_presence,
    validate_vapi_variable_values,
)


def test_build_vapi_variable_values_from_profile_and_form():
    values = build_vapi_variable_values(
        {
            "agent_name": "  Ada Agent ",
            "brokerage_name": "Ada Realty",
            "company_name": "Ada Homes",
        },
        {
            "lead_name": "Ben Miller",
            "call_purpose": "Follow up after an open house",
            "lead_context": "Attended Meadow Ranch open house",
            "property_interest": "3-bedroom townhome",
            "desired_outcome": "Book a buyer consultation",
        },
    )
    assert list(values.keys()) == list(VAPI_VARIABLE_KEYS)
    assert values["agent_name"] == "Ada Agent"
    assert values["brokerage_name"] == "Ada Realty"
    assert values["company_name"] == "Ada Homes"
    assert values["lead_name"] == "Ben Miller"
    assert values["call_purpose"] == "Follow up after an open house"
    assert values["lead_context"] == "Attended Meadow Ranch open house"
    assert values["property_interest"] == "3-bedroom townhome"
    assert values["desired_outcome"] == "Book a buyer consultation"


def test_lead_context_falls_back_to_notes():
    values = build_vapi_variable_values(
        {"agent_name": "Ada", "brokerage_name": "Realty"},
        {"lead_name": "Ben", "notes": "Open house visitor"},
    )
    assert values["lead_context"] == "Open house visitor"


def test_validate_requires_agent_lead_and_brokerage_or_company():
    assert validate_vapi_variable_values(
        {
            "agent_name": "Ada",
            "brokerage_name": "Realty",
            "company_name": "",
            "lead_name": "Ben",
        }
    ) is None
    assert validate_vapi_variable_values(
        {
            "agent_name": "Ada",
            "brokerage_name": "",
            "company_name": "Ada Homes",
            "lead_name": "Ben",
        }
    ) is None

    err = validate_vapi_variable_values(
        {"agent_name": "", "brokerage_name": "Realty", "company_name": "", "lead_name": "Ben"}
    )
    assert err and "agent name" in err.lower()

    err = validate_vapi_variable_values(
        {"agent_name": "Ada", "brokerage_name": "Realty", "company_name": "", "lead_name": ""}
    )
    assert err and "lead name" in err.lower()

    err = validate_vapi_variable_values(
        {"agent_name": "Ada", "brokerage_name": "", "company_name": "", "lead_name": "Ben"}
    )
    assert err and "brokerage" in err.lower() and "company" in err.lower()


def test_log_variable_values_presence_never_logs_values(caplog):
    secretish = {
        "agent_name": "Ada Agent",
        "brokerage_name": "Secret Brokerage",
        "company_name": "Secret Company",
        "lead_name": "Ben Miller",
        "call_purpose": "Do not log this purpose text",
        "lead_context": "Sensitive lead context must never appear",
        "property_interest": "123 Secret Lane",
        "desired_outcome": "Book appointment",
    }
    with caplog.at_level(logging.INFO):
        log_variable_values_presence(secretish)

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "Vapi variableValues presence" in joined
    for key in VAPI_VARIABLE_KEYS:
        assert key in joined
    assert "Ada Agent" not in joined
    assert "Secret Brokerage" not in joined
    assert "Ben Miller" not in joined
    assert "Sensitive lead context" not in joined
    assert "123 Secret Lane" not in joined
    assert "+1" not in joined


def test_outbound_payload_includes_assistant_overrides(monkeypatch):
    provider = VapiVoiceProvider()
    provider.api_key = "test-key"
    provider.assistant_id = "asst_lead_qualifier"
    provider.phone_number_id = "phone_123"

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id":"call_abc"}'

    def fake_urlopen(request, timeout=20):
        captured["url"] = request.full_url
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr("voice_provider.urllib.request.urlopen", fake_urlopen)

    values = build_vapi_variable_values(
        {
            "agent_name": "Ada Agent",
            "brokerage_name": "Ada Realty",
            "company_name": "Ada Homes",
        },
        {
            "lead_name": "Ben Miller",
            "phone_number": "+13038703107",
            "call_purpose": "Follow up",
            "lead_context": "Open house visitor",
            "property_interest": "Townhome",
            "desired_outcome": "Book consultation",
            "lead_type": "buyer",
        },
    )
    result = provider.start_outbound_call(
        99,
        {
            "phone_number": "+13038703107",
            "lead_type": "buyer",
        },
        {"id": 1},
        "prompt unused",
        variable_values=values,
    )
    assert result["provider_call_id"] == "call_abc"

    import json

    payload = json.loads(captured["body"].decode("utf-8"))
    assert payload["assistantId"] == "asst_lead_qualifier"
    assert payload["phoneNumberId"] == "phone_123"
    assert payload["customer"] == {"number": "+13038703107", "name": "Ben Miller"}
    assert payload["assistantOverrides"]["variableValues"] == {
        "agent_name": "Ada Agent",
        "brokerage_name": "Ada Realty",
        "company_name": "Ada Homes",
        "lead_name": "Ben Miller",
        "call_purpose": "Follow up",
        "lead_context": "Open house visitor",
        "property_interest": "Townhome",
        "desired_outcome": "Book consultation",
    }
    overrides = payload["assistantOverrides"]
    assert overrides["model"]["provider"] == "openai"
    assert overrides["model"]["model"] == "chat-latest"
    assert overrides["transcriber"] == {
        "provider": "deepgram",
        "model": "flux-general-en",
        "language": "en",
        "eotThreshold": 0.9,
        "eotTimeoutMs": 8000,
    }
    assert overrides["startSpeakingPlan"] == {"waitSeconds": 0.55}
    assert overrides["stopSpeakingPlan"] == {
        "numWords": 2,
        "voiceSeconds": 0.35,
        "backoffSeconds": 1.2,
    }
    tools = payload["assistantOverrides"]["model"]["tools"]
    tool_names = {tool["function"]["name"] for tool in tools}
    assert {
        "list_open_leads",
        "update_lead_status",
        "update_lead_sms_consent_status",
        "draft_lead_email",
    }.issubset(tool_names)
    assert all(tool["server"]["url"].endswith("/webhook/voice") for tool in tools)
    messages = payload["assistantOverrides"]["model"]["messages"]
    assert messages == [{"role": "system", "content": "prompt unused"}]


def test_live_voice_overrides_use_copilot_prompt_and_signed_static_tool_parameter():
    overrides = build_live_voice_assistant_overrides(
        {"agent_name": "Ada", "brokerage_name": "Ada Realty"},
        "signed-account-token",
    )

    assert overrides["firstMessage"] == "Hi Ada. How can I help?"
    assert overrides["firstMessageMode"] == "assistant-speaks-first"
    assert overrides["firstMessageInterruptionsEnabled"] is False
    assert "assistant.speechStarted" in overrides["clientMessages"]
    assert overrides["model"]["model"] == "chat-latest"
    prompt = overrides["model"]["messages"][0]["content"]
    assert "live CRM copilot" in prompt
    assert "Listen to the user's complete thought" in prompt
    assert "not calling a lead" in prompt
    for tool in overrides["model"]["tools"]:
        assert tool["parameters"] == [
            {"key": "topai_account_token", "value": "{{topai_account_token}}"}
        ]


def test_subscriber_app_renders_one_click_live_voice_without_private_key(
    app_client, two_users, monkeypatch
):
    import config

    u1, _ = two_users
    db.update_business_profile(
        u1, agent_name="Ada", brokerage_name="Ada Realty", company_name=""
    )
    monkeypatch.setattr(config, "VAPI_PUBLIC_API_KEY", "public-browser-key")
    monkeypatch.setattr(config, "VOICE_PROVIDER_API_KEY", "private-server-key")
    monkeypatch.setattr(config, "REAL_ESTATE_LEAD_QUALIFIER_ASSISTANT_ID", "assistant-123")
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    response = app_client.get("/app")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="topai-live-button"' in html
    assert "Ask TopAI" in html
    assert "public-browser-key" in html
    assert "assistant-123" in html
    assert "private-server-key" not in html


def test_live_voice_widget_renders_on_subscriber_and_marketing_pages(
    app_client, two_users, monkeypatch
):
    import config

    u1, _ = two_users
    db.update_business_profile(
        u1, agent_name="Ada", brokerage_name="Ada Realty", company_name=""
    )
    monkeypatch.setattr(config, "VAPI_PUBLIC_API_KEY", "public-browser-key")
    monkeypatch.setattr(config, "VOICE_PROVIDER_API_KEY", "private-server-key")
    monkeypatch.setattr(config, "REAL_ESTATE_LEAD_QUALIFIER_ASSISTANT_ID", "assistant-123")
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    for path in ("/app", "/dashboard", "/features", "/pricing", "/terms"):
        response = app_client.get(path)
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert html.count('id="topai-live-button"') == 1
        assert 'id="topai-live-config"' in html
        assert "topai_live_voice.js" in html
        assert "public-browser-key" in html
        assert "assistant-123" in html
        assert "private-server-key" not in html

def test_live_voice_button_remains_visible_when_voice_config_is_missing(
    app_client, two_users, monkeypatch
):
    import config

    u1, _ = two_users
    monkeypatch.setattr(config, "VAPI_PUBLIC_API_KEY", "")
    monkeypatch.setattr(config, "REAL_ESTATE_LEAD_QUALIFIER_ASSISTANT_ID", "")
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    response = app_client.get("/app")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert html.count('id="topai-live-button"') == 1
    assert 'id="topai-live-config"' in html
    assert '"configured": false' in html
    assert "topai_live_voice.js" in html

def test_start_voice_call_blocks_missing_business_profile(app_client, two_users):
    u1, _ = two_users
    persona_id = db.create_voice_persona(
        u1,
        {
            "name": "Qualifier",
            "persona_type": "buyer",
            "prompt": "Qualify the lead.",
            "tone": "professional",
            "goal": "Book a consult",
            "objection_handling_notes": "",
        },
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    res = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Ben Miller",
            "phone_number": "3038703107",
            "call_purpose": "Follow up",
            "lead_context": "Open house visitor",
            "property_interest": "Townhome",
            "desired_outcome": "Book consultation",
            "compliance_confirmed": True,
        },
    )
    assert res.status_code == 400
    body = res.get_json()
    assert "agent name" in body["error"].lower()

    db.update_business_profile(u1, agent_name="Ada Agent", brokerage_name="", company_name="")
    res = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Ben Miller",
            "phone_number": "3038703107",
            "compliance_confirmed": True,
        },
    )
    assert res.status_code == 400
    assert "brokerage" in res.get_json()["error"].lower()


def test_business_profile_round_trip(app_client, two_users):
    u1, _ = two_users
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    res = app_client.put(
        "/account/business-profile",
        json={
            "agent_name": "Ada Agent",
            "brokerage_name": "Ada Realty",
            "company_name": "Ada Homes",
        },
    )
    assert res.status_code == 200
    assert res.get_json()["profile"]["agent_name"] == "Ada Agent"

    res = app_client.get("/account/business-profile")
    assert res.status_code == 200
    profile = res.get_json()["profile"]
    assert profile == {
        "agent_name": "Ada Agent",
        "brokerage_name": "Ada Realty",
        "company_name": "Ada Homes",
    }
