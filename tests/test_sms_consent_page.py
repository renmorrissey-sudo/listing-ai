"""Public /sms-consent page: optional SMS opt-in for A2P verification."""

import re
from unittest.mock import patch

import db
import sms_consent
from sms_consent import SMS_CONSENT_CHECKBOX_TEXT, SMS_CONSENT_DISCLOSURE_VERSION


def test_sms_consent_page_is_public(app_client):
    res = app_client.get("/sms-consent")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'id="gate"' not in html
    assert "gate-overlay" not in html
    assert "<h2>Subscriber Access</h2>" not in html
    assert "TopAI RE Tools" in html
    assert "Sky Blue Holdings LLC" in html
    assert "(720) 903-2519" in html
    assert "real estate" in html.lower()
    assert 'name="sms_consent"' in html
    assert "checked" not in html.split('id="sms_consent"')[1].split(">")[0]
    assert SMS_CONSENT_CHECKBOX_TEXT in html
    assert "https://topairealestatetools.com/privacy" in html
    assert "https://topairealestatetools.com/terms" in html
    assert 'rel="canonical"' in html
    assert "https://topairealestatetools.com/sms-consent" in html
    assert not re.search(r'name=["\']robots["\']\s+content=["\'][^"\']*noindex', html, re.I)


def test_sms_consent_checkbox_defaults_unchecked_on_get(app_client):
    html = app_client.get("/sms-consent").get_data(as_text=True)
    # Input must not include checked attribute when page first loads.
    match = re.search(r'<input[^>]*id="sms_consent"[^>]*>', html)
    assert match
    assert "checked" not in match.group(0)


def test_inquiry_without_consent_saves_and_blocks_sms(app_client, two_users):
    phone = "+15557654321"
    res = app_client.post(
        "/sms-consent",
        data={
            "name": "Jamie Prospect",
            "phone": phone,
            "message": "Looking for a 3-bed near downtown.",
            # sms_consent intentionally omitted
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert b"inquiry was received" in res.data.lower() or b"Thanks" in res.data

    row = db.latest_sms_consent_inquiry_for_phone(phone)
    assert row is not None
    assert row["sms_consent"] is False
    assert row["name"] == "Jamie Prospect"
    assert row["phone_number"] == phone
    assert "downtown" in row["message"]
    assert row.get("consent_at") in (None, "")
    assert row.get("disclosure_version") == SMS_CONSENT_DISCLOSURE_VERSION
    assert row.get("source_url")

    blocked = sms_consent.outbound_sms_blocked_for_phone(phone)
    assert blocked
    assert "without SMS consent" in blocked

    # Outbound send path must refuse.
    u1, _ = two_users
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    personas = db.list_voice_personas(u1)
    assert personas
    persona = personas[0]
    with patch("app.get_sms_provider") as mock_get:
        mock_provider = mock_get.return_value
        mock_provider.is_configured.return_value = True
        mock_provider.send_sms.side_effect = AssertionError("must not send without consent")
        send_res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": persona["id"],
                "lead_name": "Jamie Prospect",
                "phone_number": phone,
                "message_body": "Hi Jamie — still looking?",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert send_res.status_code == 403
    assert "consent" in send_res.get_json()["error"].lower()
    mock_provider.send_sms.assert_not_called()


def test_inquiry_with_consent_records_metadata_no_auto_send(app_client):
    phone = "+15557651111"
    with patch("sms_provider.TwilioSmsProvider.send_sms") as send_sms:
        res = app_client.post(
            "/sms-consent",
            data={
                "name": "Alex Buyer",
                "phone": phone,
                "message": "Interested in 123 Main St showing times.",
                "sms_consent": "1",
            },
            headers={"User-Agent": "ConsentTestAgent/1.0"},
        )
    assert res.status_code == 200
    send_sms.assert_not_called()

    row = db.latest_sms_consent_inquiry_for_phone(phone)
    assert row["sms_consent"] is True
    assert row["consent_at"]
    assert row["disclosure_version"] == SMS_CONSENT_DISCLOSURE_VERSION
    assert "sms-consent" in (row.get("source_url") or "")
    assert row.get("user_agent")
    assert sms_consent.outbound_sms_blocked_for_phone(phone) is None
    assert sms_consent.phone_has_affirmative_sms_consent(phone) is True


def test_sms_consent_in_sitemap_and_how_it_works(app_client):
    sitemap = app_client.get("/sitemap.xml").get_data(as_text=True)
    assert "https://topairealestatetools.com/sms-consent" in sitemap
    how = app_client.get("/how-it-works").get_data(as_text=True)
    assert "/sms-consent" in how
    privacy = app_client.get("/privacy").get_data(as_text=True)
    assert "Mobile information, including telephone numbers and SMS consent records" in privacy
    assert "message frequency varies" in privacy.lower()
    assert "STOP" in privacy and "HELP" in privacy
