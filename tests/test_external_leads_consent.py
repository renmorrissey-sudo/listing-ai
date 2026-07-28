"""External lead ingestion + SMS consent authorization tests."""

from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import crm_db
import db
import external_leads_db as xdb
from external_leads.consent_workflow import confirm_qualifying_consent, save_evidence_upload
from external_leads.csv_import import commit_csv, preview_csv
from external_leads.ingest import ingest_external_lead
from external_leads.webhook import generate_webhook_secret, hash_webhook_secret, process_webhook
from sms_authorization import can_send_sms


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _source(user_id, key="portal-a", secret=None):
    return xdb.create_external_lead_source(
        user_id,
        name="Portal A",
        category="portal_inquiry",
        provider_key=key,
        import_method="webhook" if secret else "manual",
        webhook_secret_hash=hash_webhook_secret(secret) if secret else None,
        default_pond_status="claimable",
    )


def test_ingest_defaults_unverified_blocked(two_users):
    u1, _ = two_users
    sid = _source(u1)
    source = xdb.get_external_lead_source(sid, u1)
    result = ingest_external_lead(
        u1,
        {"full_name": "Jane Ext", "phone": "+15551230001", "external_record_id": "ext-1"},
        source_row=source,
        method="manual",
    )
    assert result["error"] is None
    lead = db.get_lead(result["lead_id"], u1)
    assert lead["sms_consent_status"] == "unverified"
    assert int(lead["sms_sending_blocked"]) == 1
    ok, msg = can_send_sms(u1, lead["id"])
    assert ok is False
    assert "consent" in msg.lower()


def test_csv_consent_true_stays_unverified(two_users):
    u1, _ = two_users
    sid = _source(u1, key="csv-src")
    source = xdb.get_external_lead_source(sid, u1)
    csv_text = "name,phone,consent\nSam,+15551230002,true\n"
    preview = preview_csv(csv_text)
    assert preview["invalid_in_preview"] == 0
    stats = commit_csv(u1, csv_text, preview["mapping"], source_row=source)
    assert stats["created"] == 1
    assert stats["pending_evidence"] == 1
    leads = crm_db.filter_leads(u1, external_only=True)
    assert leads
    lead = leads[0]
    assert lead["sms_consent_status"] == "unverified"
    assert int(lead["sms_sending_blocked"]) == 1
    evidence = xdb.list_consent_evidence(u1, lead["id"])
    assert evidence and evidence[0]["consent_status"] == "pending"


def test_webhook_idempotency_and_tenant_isolation(two_users):
    u1, u2 = two_users
    secret = generate_webhook_secret()
    sid1 = _source(u1, key="shared-key", secret=secret)
    _source(u2, key="shared-key", secret="other-secret-value-not-matching")
    body = {
        "name": "Webhook Lead",
        "phone": "+15551230003",
        "external_record_id": "wh-99",
        "consent_status": "yes",
    }
    result, err, status = process_webhook("shared-key", body, secret)
    assert status == 200 and err is None
    lead_id = result["lead_id"]
    lead = db.get_lead(lead_id, u1)
    assert lead is not None
    assert db.get_lead(lead_id, u2) is None
    assert lead["sms_consent_status"] == "unverified"

    result2, err2, status2 = process_webhook("shared-key", body, secret)
    assert status2 == 200 and err2 is None
    assert result2["lead_id"] == lead_id
    assert result2["action"] in {"updated", "skipped_opted_out"}

    bad, err_bad, status_bad = process_webhook("shared-key", body, "wrong-secret")
    assert status_bad == 401


def test_confirm_and_reject_consent(two_users):
    u1, _ = two_users
    sid = _source(u1, key="confirm-src")
    source = xdb.get_external_lead_source(sid, u1)
    result = ingest_external_lead(
        u1,
        {"full_name": "Confirm Me", "phone": "+15551230004"},
        source_row=source,
        method="manual",
    )
    lead_id = result["lead_id"]
    form = {
        "consent_method": "verbal",
        "attestation_accepted": "1",
        "consent_at": datetime.now(timezone.utc).isoformat(),
        "authorized_agent_name": "Agent A",
        "authorized_brokerage_name": "Brokerage B",
        "phone_number": "+15551230004",
        "evidence_type": "verbal_attestation",
        "verbal_context": "Phone call",
        "verbal_response": "Yes, you may text me",
        "disclosure_text": "May I send you SMS?",
    }
    out, err = confirm_qualifying_consent(u1, lead_id, form)
    assert err is None and out
    lead = db.get_lead(lead_id, u1)
    assert lead["sms_consent_status"] == "verified"
    assert int(lead["sms_sending_blocked"]) == 0
    ok, _ = can_send_sms(u1, lead_id)
    assert ok is True

    xdb.set_lead_sms_consent_state(
        lead_id,
        u1,
        sms_consent_status="not_permitted",
        sms_sending_blocked=True,
        actor_user_id=u1,
        source="test",
    )
    ok2, _ = can_send_sms(u1, lead_id)
    assert ok2 is False


def test_external_platform_confirm_requires_fields(two_users):
    u1, _ = two_users
    sid = _source(u1, key="plat")
    source = xdb.get_external_lead_source(sid, u1)
    lead_id = ingest_external_lead(
        u1,
        {"full_name": "Plat", "phone": "+15551230005"},
        source_row=source,
        method="manual",
    )["lead_id"]
    form = {
        "consent_method": "external_platform",
        "attestation_accepted": "1",
        "consent_at": datetime.now(timezone.utc).isoformat(),
        "authorized_agent_name": "Agent",
        "authorized_brokerage_name": "Broker",
        "phone_number": "+15551230005",
        "evidence_type": "URL",
    }
    out, err = confirm_qualifying_consent(u1, lead_id, form)
    assert out is None
    assert "Platform" in err or "platform" in err.lower() or "authorized" in err.lower()


def test_opt_out_persists_across_reingest(two_users):
    u1, _ = two_users
    sid = _source(u1, key="opt")
    source = xdb.get_external_lead_source(sid, u1)
    lead_id = ingest_external_lead(
        u1,
        {"full_name": "Opt", "phone": "+15551230006", "external_record_id": "o1"},
        source_row=source,
        method="manual",
    )["lead_id"]
    xdb.apply_opt_out_consent(lead_id, u1, source="test")
    lead = db.get_lead(lead_id, u1)
    assert lead["sms_consent_status"] == "opted_out"
    result = ingest_external_lead(
        u1,
        {
            "full_name": "Opt Updated",
            "phone": "+15551230006",
            "external_record_id": "o1",
            "original_consent_status": "true",
        },
        source_row=source,
        method="csv",
    )
    assert result["action"] == "skipped_opted_out"
    lead2 = db.get_lead(lead_id, u1)
    assert lead2["sms_consent_status"] == "opted_out"
    assert int(lead2["sms_sending_blocked"]) == 1


def test_claim_does_not_change_consent(two_users):
    u1, _ = two_users
    sid = _source(u1, key="pond")
    source = xdb.get_external_lead_source(sid, u1)
    lead_id = ingest_external_lead(
        u1,
        {"full_name": "Pond", "phone": "+15551230007", "pond_status": "claimable"},
        source_row=source,
        method="manual",
    )["lead_id"]
    before = db.get_lead(lead_id, u1)
    claimed, err = xdb.claim_lead(lead_id, u1)
    assert err is None
    assert claimed["pond_status"] == "claimed"
    assert claimed["sms_consent_status"] == before["sms_consent_status"]
    assert int(claimed["sms_sending_blocked"]) == int(before["sms_sending_blocked"])


def test_send_paths_denied_when_blocked(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    sid = _source(u1, key="send-block")
    source = xdb.get_external_lead_source(sid, u1)
    lead_id = ingest_external_lead(
        u1,
        {"full_name": "Blocked Send", "phone": "+15551230008"},
        source_row=source,
        method="manual",
    )["lead_id"]
    persona = db.list_voice_personas(u1)[0] if db.list_voice_personas(u1) else None
    if not persona:
        # defaults from init
        personas = db.list_voice_personas(None) if hasattr(db, "list_voice_personas") else []
        persona = personas[0] if personas else None
    # Use any persona available to user
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM voice_personas WHERE user_id IS NULL OR user_id = ? LIMIT 1",
            (u1,),
        ).fetchone()
    persona_id = row["id"]
    res = app_client.post(
        "/sms/messages",
        json={
            "persona_id": persona_id,
            "lead_name": "Blocked Send",
            "phone_number": "+15551230008",
            "message_body": "Hi there from TopAI",
            "send_now": True,
            "compliance_confirmed": True,
            "lead_type": "buyer",
        },
    )
    assert res.status_code == 403
    assert "consent" in res.get_json()["error"].lower()

    res_test = app_client.post(
        "/sms/test",
        json={"to": "+15551230008", "message": "test ping"},
    )
    assert res_test.status_code == 403


def test_malformed_csv_and_phone(two_users):
    u1, _ = two_users
    preview = preview_csv("not,a,valid\n,,,\n")
    assert preview["total_rows"] >= 1
    stats = commit_csv(u1, "name,phone\nBad,123\n", {"name": "name", "phone": "phone"})
    assert stats["invalid"] >= 1
    result = ingest_external_lead(u1, {"full_name": "X", "phone": "not-a-phone"}, method="manual")
    assert result["error"]


def test_upload_validation(two_users, tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "CONSENT_UPLOAD_DIR", str(tmp_path))
    u1, _ = two_users
    bad = MagicMock()
    bad.filename = "evil.exe"
    bad.mimetype = "application/octet-stream"
    bad.read = lambda n: b"x" * 10
    ref, err = save_evidence_upload(u1, bad)
    assert err and ref is None

    good = MagicMock()
    good.filename = "note.txt"
    good.mimetype = "text/plain"
    good.read = lambda n: b"hello evidence"
    ref2, err2 = save_evidence_upload(u1, good)
    assert err2 is None and ref2


def test_audit_append_only(two_users):
    u1, _ = two_users
    sid = _source(u1, key="audit")
    source = xdb.get_external_lead_source(sid, u1)
    lead_id = ingest_external_lead(
        u1,
        {"full_name": "Audit", "phone": "+15551230009"},
        source_row=source,
        method="manual",
    )["lead_id"]
    first = xdb.list_consent_audit(u1, lead_id)
    assert first
    count1 = len(first)
    xdb.append_consent_audit(
        u1, lead_id, actor_user_id=u1, action="manual_note", new_value="note"
    )
    second = xdb.list_consent_audit(u1, lead_id)
    assert len(second) == count1 + 1
    # No delete API — events only grow
    assert {a["id"] for a in first}.issubset({a["id"] for a in second})


def test_migration_additive_columns_present(two_users):
    u1, _ = two_users
    with db.get_db() as conn:
        info = conn.execute("PRAGMA table_info(leads)").fetchall()
        cols = {dict(r)["name"] for r in info}
    for col in (
        "sms_consent_status",
        "sms_sending_blocked",
        "external_source_id",
        "pond_status",
        "email",
    ):
        assert col in cols
    with db.get_db() as conn:
        tables = {
            dict(r)["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for t in (
        "external_lead_sources",
        "sms_consent_evidence",
        "consent_audit_events",
        "external_lead_import_batches",
    ):
        assert t in tables


def test_consent_pages_render(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    assert app_client.get("/crm/external-sources").status_code == 200
    assert app_client.get("/crm/external-leads/new").status_code == 200
    assert app_client.get("/crm/external-leads/import").status_code == 200
    sid = _source(u1, key="ui")
    source = xdb.get_external_lead_source(sid, u1)
    lead_id = ingest_external_lead(
        u1,
        {"full_name": "UI Lead", "phone": "+15551230010"},
        source_row=source,
        method="manual",
    )["lead_id"]
    page = app_client.get(f"/crm/leads/{lead_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "SMS consent" in html or "Unverified" in html
    consent = app_client.get(f"/crm/leads/{lead_id}/consent")
    assert consent.status_code == 200
