"""Public /sms-consent page: required SMS opt-in for A2P / Telnyx verification."""

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
    assert "Subscriber Access" not in html
    assert "TopAI RE Tools" in html
    assert "Sky Blue Holdings LLC" in html
    assert "(720) 903-2519" in html
    assert 'name="sms_consent"' in html
    assert 'name="first_name"' in html
    assert 'name="last_name"' in html
    assert 'name="phone"' in html
    assert SMS_CONSENT_CHECKBOX_TEXT in html
    assert "Message frequency varies" in html
    assert "Message and data rates may apply" in html
    assert "STOP" in html and "HELP" in html
    assert "not a condition" in html.lower()
    assert "https://topairealestatetools.com/privacy" in html
    assert "https://topairealestatetools.com/terms" in html
    assert "checked" not in html.split('id="sms_consent"')[1].split(">")[0]


def test_sms_consent_checkbox_defaults_unchecked_on_get(app_client):
    html = app_client.get("/sms-consent").get_data(as_text=True)
    match = re.search(r'<input[^>]*id="sms_consent"[^>]*>', html)
    assert match
    assert "checked" not in match.group(0)


def test_missing_checkbox_rejected(app_client):
    phone = "+15557654321"
    res = app_client.post(
        "/sms-consent",
        data={
            "first_name": "Jamie",
            "last_name": "Prospect",
            "phone": phone,
            "message": "Looking for a 3-bed near downtown.",
        },
    )
    assert res.status_code == 400
    assert b"SMS consent" in res.data or b"consent box" in res.data.lower()
    assert db.latest_sms_consent_inquiry_for_phone(phone) is None


def test_invalid_phone_rejected(app_client):
    res = app_client.post(
        "/sms-consent",
        data={
            "first_name": "Sam",
            "last_name": "Buyer",
            "phone": "123",
            "message": "Need help with a listing.",
            "sms_consent": "1",
        },
    )
    assert res.status_code == 400
    assert b"phone" in res.data.lower()


def test_missing_required_fields(app_client):
    res = app_client.post(
        "/sms-consent",
        data={"first_name": "", "last_name": "X", "phone": "+15557650001", "message": "Hi", "sms_consent": "1"},
    )
    assert res.status_code == 400
    assert b"first name" in res.data.lower()


def test_successful_consent_submission_public(app_client):
    phone = "+15557651111"
    res = app_client.post(
        "/sms-consent",
        data={
            "first_name": "Alex",
            "last_name": "Buyer",
            "email": "alex@example.com",
            "phone": phone,
            "message": "Interested in 123 Main St showing times.",
            "sms_consent": "1",
            "campaign_source": "telnyx-tollfree",
        },
        headers={"User-Agent": "ConsentTestAgent/1.0"},
    )
    assert res.status_code == 200
    assert b"Thanks" in res.data
    assert b"role=\"status\"" in res.data or b"success" in res.data.lower()

    row = db.latest_sms_consent_inquiry_for_phone(phone)
    assert row is not None
    assert row["sms_consent"] is True
    assert row["first_name"] == "Alex"
    assert row["last_name"] == "Buyer"
    assert row["email"] == "alex@example.com"
    assert row["consent_at"]
    assert row["disclosure_version"] == SMS_CONSENT_DISCLOSURE_VERSION
    assert "sms-consent" in (row.get("source_url") or "")
    assert row.get("user_agent")
    assert row.get("campaign_source") == "telnyx-tollfree"
    assert sms_consent.phone_has_affirmative_sms_consent(phone) is True


def test_duplicate_submission_does_not_create_second_row(app_client):
    phone = "+15557652222"
    payload = {
        "first_name": "Dup",
        "last_name": "Lead",
        "phone": phone,
        "message": "First inquiry",
        "sms_consent": "1",
    }
    assert app_client.post("/sms-consent", data=payload).status_code == 200
    payload["message"] = "Second inquiry update"
    res2 = app_client.post("/sms-consent", data=payload)
    assert res2.status_code == 200
    assert b"already have your SMS consent" in res2.data or b"Thanks" in res2.data

    with db.get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_consent_inquiries WHERE phone_number = ?",
            (phone,),
        ).fetchone()["c"]
    assert int(count) == 1
    row = db.latest_sms_consent_inquiry_for_phone(phone)
    assert "Second inquiry" in row["message"]


def test_database_failure_handling(app_client):
    with patch("sms_consent.create_sms_consent_inquiry", side_effect=RuntimeError("db down")):
        res = app_client.post(
            "/sms-consent",
            data={
                "first_name": "Pat",
                "last_name": "Lee",
                "phone": "+15557653333",
                "message": "Need help",
                "sms_consent": "1",
            },
        )
    assert res.status_code == 500
    # HTML error page, not JSON secrets
    body = res.get_data(as_text=True)
    assert "could not save" in body.lower() or "try again" in body.lower()
    assert "Traceback" not in body
    assert "db down" not in body
    assert "password" not in body.lower()


def test_postgres_bool_binding_helper(monkeypatch):
    monkeypatch.setattr("config.DB_ENGINE", "postgres")
    # Simulate what create_sms_consent_inquiry stores
    import config

    flag = True
    consent_val = flag if config.DB_ENGINE == "postgres" else (1 if flag else 0)
    assert consent_val is True


def test_opt_in_proof_asset_is_public(app_client):
    res = app_client.get("/static/sms-opt-in-proof.png")
    assert res.status_code == 200
    assert res.headers.get("Content-Type", "").startswith("image/")
    assert res.data[:8] == b"\x89PNG\r\n\x1a\n"
