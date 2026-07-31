"""PostgreSQL integration: full pre-provider SMS prepare path.

Requires TEST_DATABASE_URL (postgresql://...). Skipped otherwise.
Does not call real Telnyx.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="Set TEST_DATABASE_URL for PostgreSQL SMS prepare integration",
)


@pytest.fixture
def pg_sms_env(monkeypatch):
    """Point the app at a real Postgres DB and re-init schema."""
    url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SUBSCRIPTION_REQUIRED", "false")

    import importlib

    import config

    importlib.reload(config)
    assert config.DB_ENGINE == "postgres"

    import db

    # Isolate from other tests sharing the same Postgres instance.
    suffix = uuid.uuid4().hex[:8]
    db.init_db()
    # Clear shared unique sender numbers so ensure_telnyx_platform_sender can run.
    with db.get_db() as conn:
        conn.execute("DELETE FROM tenant_sms_senders")
    yield {"db": db, "config": config, "suffix": suffix}


def _seed_user_and_persona(db, config, suffix):
    import auth
    import tenant_sms_db as tdb

    email = f"pg-sms-{suffix}@example.com"
    uid = db.create_user(email, auth.hash_password("password123"))
    db.update_user_subscription(uid, "active")
    tdb.accept_sms_terms(uid, uid)

    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_personas
                (user_id, name, persona_type, prompt, tone, goal,
                 objection_handling_notes, is_default, active, created_at)
            VALUES (NULL, ?, 'buyer', 'prompt', 'professional', 'qualify', '', 1, 1, ?)
            """,
            (f"Persona-{suffix}", now),
        )
        persona_id = cur.lastrowid
    return uid, persona_id


def _telnyx_ready(config, monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_TEST_ONLY")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", False)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    monkeypatch.setattr(config, "APP_URL", "https://example.com")


@pytest.mark.parametrize("touch_sms,touch_call", [(True, False), (False, False), (True, True)])
def test_postgres_update_lead_contact_null_notes_and_bool_flags(
    pg_sms_env, monkeypatch, touch_sms, touch_call
):
    """Regression for e8471214e348: NULL notes + bool CASE WHEN on Postgres."""
    db = pg_sms_env["db"]
    config = pg_sms_env["config"]
    suffix = pg_sms_env["suffix"]
    from lead_service import upsert_crm_lead

    uid, _persona_id = _seed_user_and_persona(
        db, config, suffix + f"{int(touch_sms)}{int(touch_call)}"
    )
    # E.164 digits only (hex in uuid would be stripped by normalize_phone_e164).
    phone = f"+1303{uuid.uuid4().int % 10_000_000:07d}"

    lid, created, lead = upsert_crm_lead(
        uid, phone, {"lead_name": "Existing Lead"}, touch_sms=False
    )
    assert created is True
    assert lead["phone_number"] == phone

    # Existing-lead upsert with notes=NULL (typical AI SMS compose payload).
    lid2, created2, lead2 = upsert_crm_lead(
        uid,
        phone,
        {"lead_name": "Existing Lead", "lead_type": "buyer"},
        source="sms",
        touch_sms=touch_sms,
        assigned_user_id=uid,
    )
    assert created2 is False
    assert lid2 == lid

    # Explicit false/true touch flags with null notes (the IndeterminateDatatype path).
    db.update_lead_contact_fields(
        lid,
        uid,
        name="Existing Lead",
        lead_type="buyer",
        notes=None,
        touch_sms=touch_sms,
        touch_call=touch_call,
    )
    refreshed = db.get_lead(lid, uid)
    assert refreshed is not None
    if touch_sms or touch_call:
        assert refreshed.get("last_contacted_at")
        assert refreshed.get("last_outbound_at")
    if touch_call:
        assert refreshed.get("latest_call_at")


def test_postgres_full_pre_provider_sms_path_reaches_mocked_telnyx(pg_sms_env, monkeypatch):
    """
    Create existing lead → upsert (consent/touch) → attestation/audit →
    outbound message row → mocked Telnyx. No real provider call.
    """
    db = pg_sms_env["db"]
    config = pg_sms_env["config"]
    suffix = pg_sms_env["suffix"]
    from lead_service import upsert_crm_lead
    from sms_outbound import send_authorized_sms
    from sms_providers.telnyx import TelnyxSMSProvider
    import tenant_sms_db as tdb

    uid, persona_id = _seed_user_and_persona(db, config, suffix)
    _telnyx_ready(config, monkeypatch)
    # Prefer a dedicated platform sender for this user. If the global From number
    # is already claimed by another tenant row (shared test DB), require_tenant_sender
    # still falls back to the synthetic Telnyx platform sender.
    try:
        tdb.ensure_telnyx_platform_sender(uid)
    except Exception:
        pass
    # Valid US E.164: +1 720 555 XXXX (unique per run).
    phone = f"+1720555{uuid.uuid4().int % 10000:04d}"

    lid, created, _lead = upsert_crm_lead(
        uid, phone, {"lead_name": "Pat"}, touch_sms=False
    )
    assert created is True

    # Same upsert path used by POST /sms/messages (existing lead, notes often NULL).
    lid2, created2, lead2 = upsert_crm_lead(
        uid,
        phone,
        {
            "lead_name": "Pat",
            "lead_type": "buyer",
            "message_body": "Hello from Postgres prepare test",
        },
        source="sms",
        touch_sms=True,
        assigned_user_id=uid,
    )
    assert created2 is False
    assert lid2 == lid
    assert lead2["last_outbound_at"]
    assert lead2["phone_number"] == phone

    # False then true touch flags must both succeed on Postgres.
    db.update_lead_contact_fields(lid, uid, notes=None, touch_sms=False, touch_call=False)
    db.update_lead_contact_fields(lid, uid, notes=None, touch_sms=True, touch_call=False)

    provider_calls = []

    def fake_send(self, to_number, body, from_number=None, status_callback=None):
        provider_calls.append(
            {"to": to_number, "from": from_number, "body": body}
        )
        return {
            "sid": "msg_pg_prepare_1",
            "status": "queued",
            "provider_message_id": "msg_pg_prepare_1",
            "to": to_number,
            "from": from_number,
        }

    with patch.object(TelnyxSMSProvider, "send_message", fake_send):
        result, err, status = send_authorized_sms(
            uid,
            lid,
            "Hello from Postgres prepare test",
            source_page="ai_sms_compose",
            compliance_confirmed=True,
            persona_id=persona_id,
        )

    assert err is None, (result, db.get_lead(lid, uid))
    assert status == 201
    assert result["provider_message_id"] == "msg_pg_prepare_1"
    assert result["to_number"] == phone
    assert len(provider_calls) == 1
    assert provider_calls[0]["to"] == phone

    # Consent persisted via attestation path.
    lead = db.get_lead(lid, uid)
    assert lead["sms_consent_status"] == "user_certified"
    assert lead["sms_sending_blocked"] in (False, 0)

    # Outbound attempt / message row created before/during provider call.
    with db.get_db() as conn:
        msg = conn.execute(
            "SELECT * FROM sms_messages WHERE lead_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (lid, uid),
        ).fetchone()
    assert msg is not None
    assert msg["provider_message_id"] == "msg_pg_prepare_1"
    assert msg["status"] == "queued"

    # Audit trail written (attestation / send) without crashing.
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT action FROM sms_audit_events WHERE user_id = ? AND lead_id = ?",
            (uid, lid),
        ).fetchall()
        audits = [r["action"] for r in rows]
    assert "consent_certification_accepted" in audits

    # HTTP path: second prepare on existing lead must not raise IndeterminateDatatype.
    from app import app, limiter

    limiter.enabled = False
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["session_version"] = 1

    provider_calls.clear()
    with patch.object(TelnyxSMSProvider, "send_message", fake_send):
        res = client.post(
            "/sms/messages",
            json={
                "persona_id": persona_id,
                "lead_name": "Pat",
                "phone_number": phone,
                "message_body": "Second Postgres prepare send",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201, res.get_json()
    data = res.get_json()
    assert data.get("stage") != "database"
    assert "could not prepare" not in (data.get("error") or "").lower()
    assert data["provider_message_id"] == "msg_pg_prepare_1"
    assert len(provider_calls) == 1
    # Ensure no Boolean/datatype error leaked.
    assert "IndeterminateDatatype" not in str(data)
    assert "boolean" not in (data.get("error") or "").lower()
