"""CSV external-lead import page and pipeline tests."""

from io import BytesIO
from unittest.mock import patch

import db
import external_leads_db as xdb
from external_leads.csv_import import (
    CSV_MAX_BYTES,
    commit_csv,
    decode_csv_bytes,
    neutralize_formula,
    preview_csv,
    sample_csv_text,
)
from external_leads.ingest import ingest_external_lead
from sms_authorization import can_send_sms


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _source(user_id, key="csv-portal"):
    return xdb.create_external_lead_source(
        user_id,
        name="CSV Portal",
        category="portal_inquiry",
        provider_key=key,
        import_method="manual",
        default_pond_status="claimable",
    )


def test_import_get_200_authenticated(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    resp = app_client.get("/crm/external-leads/import")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "CSV import" in html
    assert "drag" in html.lower() or "drop" in html.lower()
    assert "sample CSV" in html or "sample.csv" in html
    assert "Duplicate handling" in html
    assert "consent" in html.lower()
    assert "No import history yet" in html or "Recent imports" in html
    assert "twilio" not in html.lower()
    assert "DATABASE_URL" not in html
    assert "SECRET" not in html


def test_import_unauth_redirects_to_login(app_client):
    resp = app_client.get("/crm/external-leads/import", follow_redirects=False)
    assert resp.status_code in {302, 303}
    loc = resp.headers.get("Location") or ""
    assert "/login" in loc
    assert "next=" in loc
    assert "external-leads/import" in loc


def test_sample_csv_download(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    resp = app_client.get("/crm/external-leads/import/sample.csv")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "first_name" in body
    assert "phone" in body
    assert "Alex" in body
    assert "7202891700" in body
    assert body.count("\n") >= 2


def test_sample_csv_helper_format():
    text = sample_csv_text()
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert "phone" in lines[0]
    assert "7202891700" in lines[1]


def test_preview_and_import_e164_and_blocked(app_client, two_users):
    u1, _ = two_users
    sid = _source(u1, key="e164")
    source = xdb.get_external_lead_source(sid, u1)
    csv_text = "name,phone,email,consent\nPat Example,7202891700,pat@example.com,true\n"
    preview = preview_csv(csv_text, user_id=u1, duplicate_mode="skip")
    assert preview["invalid_in_preview"] == 0
    assert preview["preview"][0]["phone_normalized"] == "+17202891700"
    assert preview["preview"][0]["valid_phone"] is True

    with patch("sms_outbound.send_authorized_sms") as send_mock:
        stats = commit_csv(
            u1, csv_text, preview["mapping"], source_row=source, duplicate_mode="skip"
        )
        send_mock.assert_not_called()
    assert stats["created"] == 1
    assert stats["sms_sent"] == 0
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? ORDER BY id DESC LIMIT 1", (u1,)
        ).fetchone()
    lead = dict(row)
    assert lead["phone_number"] == "+17202891700"
    assert lead["sms_consent_status"] in {"not_certified", "unverified"}
    assert int(lead["sms_sending_blocked"]) == 1
    ok, _msg = can_send_sms(u1, lead["id"])
    assert ok is False


def test_invalid_phone_flagged():
    preview = preview_csv("name,phone\nBad,123\n")
    assert preview["invalid_in_preview"] == 1
    assert preview["preview"][0]["valid_phone"] is False


def test_duplicate_skip_default(two_users):
    u1, _ = two_users
    sid = _source(u1, key="dup")
    source = xdb.get_external_lead_source(sid, u1)
    csv_text = "name,phone\nOne,7202891701\n"
    mapping = preview_csv(csv_text)["mapping"]
    first = commit_csv(u1, csv_text, mapping, source_row=source, duplicate_mode="skip")
    assert first["created"] == 1
    second = commit_csv(u1, csv_text, mapping, source_row=source, duplicate_mode="skip")
    assert second["skipped"] == 1
    assert second["created"] == 0
    assert second["updated"] == 0


def test_duplicate_update_mode(two_users):
    u1, _ = two_users
    sid = _source(u1, key="dup-up")
    source = xdb.get_external_lead_source(sid, u1)
    csv1 = "name,phone,email\nOne,7202891702,one@example.com\n"
    mapping = preview_csv(csv1)["mapping"]
    commit_csv(u1, csv1, mapping, source_row=source, duplicate_mode="skip")
    csv2 = "name,phone,email\nOne Updated,7202891702,one@example.com\n"
    stats = commit_csv(u1, csv2, mapping, source_row=source, duplicate_mode="update")
    assert stats["updated"] == 1
    with db.get_db() as conn:
        lead = dict(
            conn.execute(
                "SELECT * FROM leads WHERE user_id = ? AND phone_number = ?",
                (u1, "+17202891702"),
            ).fetchone()
        )
    assert "Updated" in lead["name"]
    assert int(lead["sms_sending_blocked"]) == 1


def test_reject_non_csv_upload(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    resp = app_client.post(
        "/crm/external-leads/import",
        data={
            "action": "preview",
            "csv_file": (BytesIO(b"name,phone\nA,7202891700\n"), "leads.xlsx"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert "Only .csv" in resp.get_data(as_text=True)


def test_reject_oversized_csv():
    raw = b"a" * (CSV_MAX_BYTES + 10)
    text, err = decode_csv_bytes(raw)
    assert text is None
    assert "MB" in (err or "")


def test_utf8_bom_supported(two_users):
    u1, _ = two_users
    sid = _source(u1, key="bom")
    source = xdb.get_external_lead_source(sid, u1)
    raw = "\ufeffname,phone\nBom Lead,7202891703\n".encode("utf-8-sig")
    text, err = decode_csv_bytes(raw)
    assert err is None
    preview = preview_csv(text)
    assert preview["preview"][0]["phone_normalized"] == "+17202891703"
    stats = commit_csv(u1, text, preview["mapping"], source_row=source)
    assert stats["created"] == 1


def test_formula_injection_neutralized():
    assert neutralize_formula("=CMD()") == "'=CMD()"
    assert neutralize_formula("@SUM(A1)") == "'@SUM(A1)"
    assert neutralize_formula("+17202891700") == "+17202891700"
    assert neutralize_formula("7202891700") == "7202891700"
    preview = preview_csv("name,phone,notes\nX,7202891704,=HYPERLINK(\"http://evil\")\n")
    notes = preview["preview"][0]["mapped"].get("inquiry_notes") or preview["preview"][0]["mapped"].get(
        "notes"
    )
    # notes maps to inquiry_notes via alias
    mapped_notes = preview["preview"][0]["mapped"].get("inquiry_notes", "")
    assert mapped_notes.startswith("'=") or mapped_notes.startswith("'")


def test_cross_tenant_cannot_override(two_users):
    u1, u2 = two_users
    sid1 = _source(u1, key="t1")
    source1 = xdb.get_external_lead_source(sid1, u1)
    csv_text = (
        "name,phone,user_id,tenant_id\n"
        f"Cross,7202891705,{u2},{u2}\n"
    )
    preview = preview_csv(csv_text)
    stats = commit_csv(u1, csv_text, preview["mapping"], source_row=source1)
    assert stats["created"] == 1
    with db.get_db() as conn:
        lead = dict(
            conn.execute(
                "SELECT * FROM leads WHERE phone_number = ?",
                ("+17202891705",),
            ).fetchone()
        )
    assert lead["user_id"] == u1
    assert db.get_lead(lead["id"], u2) is None


def test_page_works_without_sms_worker_or_verification(app_client, two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr("config.SMS_PROVIDER", "telnyx")
    monkeypatch.delattr("config", "TELNYX_API_KEY", raising=False)
    _login(app_client, u1)
    # Empty sources / empty history must still render
    resp = app_client.get("/crm/external-leads/import")
    assert resp.status_code == 200
    assert "Something went wrong" not in resp.get_data(as_text=True)


def test_leads_page_links_to_csv_import(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    resp = app_client.get("/crm/leads")
    assert resp.status_code == 200
    assert "/crm/external-leads/import" in resp.get_data(as_text=True)


def test_no_auto_sms_on_route_commit(app_client, two_users):
    u1, _ = two_users
    _source(u1, key="route-commit")
    _login(app_client, u1)
    with patch("sms_outbound.send_authorized_sms") as send_mock:
        resp = app_client.post(
            "/crm/external-leads/import",
            data={
                "action": "commit",
                "duplicate_mode": "skip",
                "csv_text": "name,phone\nRoute Lead,7202891706\n",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        send_mock.assert_not_called()
    html = resp.get_data(as_text=True)
    assert "No SMS was sent" in html or "Blocked" in html or "created" in html.lower()


def test_error_csv_on_invalid_rows(two_users):
    u1, _ = two_users
    sid = _source(u1, key="err")
    source = xdb.get_external_lead_source(sid, u1)
    csv_text = "name,phone\nGood,7202891707\nBad,abc\n"
    mapping = preview_csv(csv_text)["mapping"]
    stats = commit_csv(u1, csv_text, mapping, source_row=source)
    assert stats["created"] == 1
    assert stats["invalid"] == 1
    assert "row,error" in stats["error_csv"]
    assert "abc" in stats["error_csv"] or "phone" in stats["error_csv"].lower()


def test_list_active_sources_uses_portable_bool(two_users):
    """Regression for fe9ecfaf608c: active boolean compared portably."""
    u1, _ = two_users
    _source(u1, key="active-bool")
    rows = xdb.list_external_lead_sources(u1, active_only=True)
    assert len(rows) >= 1
    assert rows[0]["provider_key"] == "active-bool"
