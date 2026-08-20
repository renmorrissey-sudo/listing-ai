from lead_display import (
    is_hidden_lead_source,
    public_lead_source,
    show_external_source_badge,
)


def test_ask_topai_sources_are_hidden():
    for value in ("ask_topai", "external:ask_topai", "EXTERNAL:ASK_TOPAI"):
        assert is_hidden_lead_source(value) is True
        assert public_lead_source(value) is None
        assert show_external_source_badge({"source": value, "external_source_id": None}) is False


def test_real_external_and_sms_sources_remain_visible():
    assert public_lead_source("external:zillow") == "external:zillow"
    assert public_lead_source("sms") == "sms"
    assert public_lead_source(None) is None
    assert show_external_source_badge({"source": "external:zillow", "external_source_id": None}) is True
    assert show_external_source_badge({"source": "sms", "external_source_id": None}) is False
    assert show_external_source_badge({"source": "external:ask_topai", "external_source_id": 99}) is False
