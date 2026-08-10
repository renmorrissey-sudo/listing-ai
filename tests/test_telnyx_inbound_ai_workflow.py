"""End-to-end tests for the Telnyx two-way AI SMS conversation workflow.

Covers: webhook envelope parsing, tenant + lead matching, exactly-once inbound
persistence, AI auto-reply generation/sending, duplicate-webhook protection,
delivery-status routing, STOP compliance, and failure handling.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import config
import db
import sms_ai_agent
import sms_coach
import tenant_sms_db as tdb
import telnyx_webhooks as txwh
from sms_providers.telnyx import TelnyxSMSProvider


def _analysis(draft="Happy to help! When is a good time for a quick call?"):
    return {
        "summary": "Lead asked about the property.",
        "intent": "question",
        "recommended_next_action": "Reply and offer a call.",
        "draft_reply": draft,
        "confidence": 0.9,
        "confidence_score": 0.9,
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "suggested_lead_status": "contacted",
        "suggested_follow_up_at": None,
        "suggested_follow_up_reason": "",
        "suggested_tasks": [],
        "appointment_requested": False,
        "appointment_details": None,
        "needs_attention_reasons": [],
        "next_best_step": "Reply and offer a call.",
        "recommended_action": "Reply and offer a call.",
        "suggested_reply": draft,
        "home_value_pitch": None,
        "raw_json": None,
    }


def _unique_e164(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _setup_tenant(user_id, monkeypatch, sender_number=None):
    """Register a unique Telnyx receiving number for this tenant + configure sends."""
    account = sender_number or _unique_e164("888")
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", account)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", False)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    monkeypatch.setattr(config, "SMS_AI_AUTO_REPLY_ENABLED", True)
    tdb.upsert_tenant_sender(
        user_id,
        sender_number=account,
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    return account


def _lead(user_id, phone, name="Test Lead"):
    from lead_service import upsert_crm_lead

    lead_id, _, lead = upsert_crm_lead(
        user_id,
        phone,
        {"name": name, "phone_number": phone},
        source="test",
        touch_sms=True,
        assigned_user_id=user_id,
    )
    return lead_id, lead


def _inbound_payload(contact, account, text="Hi, is the house still available?"):
    return {
        "data": {
            "event_type": "message.received",
            "id": f"evt-{uuid.uuid4().hex[:12]}",
            "occurred_at": "2026-08-10T18:00:00.000Z",
            "payload": {
                "id": f"tx-{uuid.uuid4().hex[:12]}",
                "direction": "inbound",
                "from": {"phone_number": contact},
                "to": [{"phone_number": account}],
                "text": text,
            },
        }
    }


# 1–3, 5: envelope accepted, sender from payload.from, recipient from payload.to[0],
# inbound persisted.
def test_inbound_received_accepted_and_persisted(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164()
    payload = _inbound_payload(contact, account)
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200 and result["ok"] is True

    row = db.get_sms_message(result["message_id"], u1)
    assert row["direction"] == "inbound"
    assert row["phone_number"] == contact  # from payload.from.phone_number, NOT payload.to
    assert row["message_body"] == "Hi, is the house still available?"
    assert row["provider_message_id"] == payload["data"]["payload"]["id"]
    lead = db.get_lead(result["lead_id"], u1)
    assert lead is not None
    assert lead.get("last_inbound_at")  # last-activity updated


# 4: formatted stored phone still matches the normalized E.164 sender.
def test_formatted_lead_phone_matches_e164_inbound(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("720")
    national = contact[2:]  # strip +1
    formatted = f"({national[:3]}) {national[3:6]}-{national[6:]}"
    lead_id, _ = _lead(u1, contact)
    # Simulate a legacy/manually-formatted stored phone number.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET phone_number = ? WHERE id = ?", (formatted, lead_id)
        )

    result, status = txwh.handle_messaging_webhook(_inbound_payload(contact, account))
    assert status == 200
    assert result["lead_id"] == lead_id  # matched, no duplicate lead created


# 6: an existing conversation (lead) is reused; messages stay on one thread.
def test_existing_conversation_reused(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("640")
    lead_id, _ = _lead(u1, contact)
    out_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Test Lead", "phone_number": contact, "message_body": "First outreach"},
        status="sent",
        lead_id=lead_id,
        direction="outbound",
    )

    result, status = txwh.handle_messaging_webhook(
        _inbound_payload(contact, account, text="Yes I am interested")
    )
    assert status == 200
    assert result["lead_id"] == lead_id

    messages = db.list_lead_messages(u1, lead_id)
    ids = [m["id"] for m in messages]
    assert out_id in ids and result["message_id"] in ids
    # Chronological order: outbound first, inbound after.
    assert ids.index(out_id) < ids.index(result["message_id"])


# 7 + 8: AI receives conversation history and exactly one reply is sent.
def test_ai_reply_receives_history_and_sends_once(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("650")
    lead_id, _ = _lead(u1, contact)
    db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Test Lead", "phone_number": contact, "message_body": "Initial outreach about 12 Main St"},
        status="sent",
        lead_id=lead_id,
        direction="outbound",
    )
    result, _ = txwh.handle_messaging_webhook(
        _inbound_payload(contact, account, text="Tell me more")
    )
    inbound_id = result["message_id"]

    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()) as coach, \
         patch.object(TelnyxSMSProvider, "send_message") as send:
        send.return_value = {"provider_message_id": f"tx-out-{uuid.uuid4().hex[:8]}", "status": "queued"}
        outcome = sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Tell me more", account)

    assert outcome["replied"] is True
    prompt = coach.call_args[0][0]
    assert "Initial outreach about 12 Main St" in prompt  # history included
    assert "Tell me more" in prompt
    assert send.call_count == 1
    kwargs = send.call_args.kwargs
    assert kwargs["to_number"] == contact
    assert kwargs["from_number"] == account  # same number that received the SMS

    reply = db.get_sms_message(outcome["message_id"], u1)
    assert reply["direction"] == "outbound"
    assert reply["reply_to_message_id"] == inbound_id
    assert reply["provider_message_id"]  # Telnyx message ID stored
    assert reply["message_body"] == _analysis()["draft_reply"]


# 9: a duplicate webhook event never generates another reply.
def test_duplicate_webhook_event_no_second_reply(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("660")
    payload = _inbound_payload(contact, account)

    with patch.object(sms_ai_agent, "schedule_inbound_ai") as sched:
        r1, s1 = txwh.handle_messaging_webhook(payload, app=object())
        r2, s2 = txwh.handle_messaging_webhook(payload, app=object())
    assert s1 == 200 and s2 == 200
    assert r2.get("duplicate") is True
    assert sched.call_count == 1

    # Even if processing raced past event dedup, the reply slot itself is unique.
    inbound_id = r1["message_id"]
    lead_id = r1["lead_id"]
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch.object(TelnyxSMSProvider, "send_message") as send:
        send.return_value = {"provider_message_id": f"tx-{uuid.uuid4().hex[:8]}", "status": "queued"}
        first = sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Hi", account)
        second = sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Hi", account)
    assert first["replied"] is True
    assert second["replied"] is False and second["reason"] == "already_replied"
    assert send.call_count == 1


# 10 + 11: message.sent / message.finalized never trigger the AI.
def test_delivery_events_do_not_trigger_ai(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("670")
    lead_id, _ = _lead(u1, contact)
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Test Lead", "phone_number": contact, "message_body": "Out"},
        status="queued",
        lead_id=lead_id,
        direction="outbound",
    )
    pmid = f"tx-dlr-{uuid.uuid4().hex[:8]}"
    db.update_sms_message_send_result(mid, provider_message_id=pmid, status="queued")

    for event_type, to_status in (("message.sent", "sent"), ("message.finalized", "delivered")):
        payload = {
            "data": {
                "event_type": event_type,
                "id": f"evt-{uuid.uuid4().hex[:10]}",
                "payload": {
                    "id": pmid,
                    "from": {"phone_number": account},
                    "to": [{"phone_number": contact, "status": to_status}],
                },
            }
        }
        with patch.object(sms_ai_agent, "schedule_inbound_ai") as sched, \
             patch.object(sms_ai_agent, "process_inbound_ai") as proc:
            result, status = txwh.handle_messaging_webhook(payload, app=object())
        assert status == 200
        assert not sched.called and not proc.called


# 12: message.finalized updates the existing outbound row, no new timeline entry.
def test_finalized_updates_existing_message(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("680")
    lead_id, _ = _lead(u1, contact)
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Test Lead", "phone_number": contact, "message_body": "Out"},
        status="queued",
        lead_id=lead_id,
        direction="outbound",
    )
    pmid = f"tx-fin-{uuid.uuid4().hex[:8]}"
    db.update_sms_message_send_result(mid, provider_message_id=pmid, status="queued")
    before = len(db.list_lead_messages(u1, lead_id))

    payload = {
        "data": {
            "event_type": "message.finalized",
            "id": f"evt-{uuid.uuid4().hex[:10]}",
            "payload": {
                "id": pmid,
                "from": {"phone_number": account},
                "to": [{"phone_number": contact, "status": "delivered"}],
            },
        }
    }
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200

    row = db.get_sms_message(mid, u1)
    assert row["status"] == "delivered"
    assert row["delivered_at"]
    assert len(db.list_lead_messages(u1, lead_id)) == before  # same row updated

    # Out-of-order late "sent" webhook must not regress delivered.
    late = {
        "data": {
            "event_type": "message.sent",
            "id": f"evt-{uuid.uuid4().hex[:10]}",
            "payload": {
                "id": pmid,
                "from": {"phone_number": account},
                "to": [{"phone_number": contact, "status": "sent"}],
            },
        }
    }
    txwh.handle_messaging_webhook(late)
    assert db.get_sms_message(mid, u1)["status"] == "delivered"


# 13: STOP (including punctuated forms) disables automated replies.
def test_stop_disables_automated_replies(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("690")

    with patch.object(sms_ai_agent, "schedule_inbound_ai") as sched:
        result, status = txwh.handle_messaging_webhook(
            _inbound_payload(contact, account, text="Stop."), app=object()
        )
    assert status == 200
    assert not sched.called  # STOP is never fed to the AI
    lead_id = result["lead_id"]
    assert tdb.is_suppressed(u1, contact)

    # Even a direct AI attempt afterwards is refused.
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch.object(TelnyxSMSProvider, "send_message") as send:
        outcome = sms_ai_agent.process_inbound_ai(
            u1, lead_id, result["message_id"], "Stop.", account
        )
    assert outcome["replied"] is False
    assert not send.called


# 14: unknown destination is deterministic and safe (acked, nothing stored).
def test_unknown_destination_is_safe(two_users, monkeypatch):
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    contact = _unique_e164("695")
    payload = _inbound_payload(contact, account="+19995550000")
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    assert result.get("ignored") == "unknown_destination"
    assert db.get_sms_message_by_provider_id(payload["data"]["payload"]["id"]) is None


# 15: tenant isolation — one agent's number never matches another agent's lead.
def test_tenant_isolation_by_receiving_number(two_users, monkeypatch):
    u1, u2 = two_users
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")  # dedicated numbers, no platform routing
    n1, n2 = _unique_e164("881"), _unique_e164("882")
    tdb.upsert_tenant_sender(u1, sender_number=n1, sms_provider="telnyx", sms_enabled=True, registration_status="verified")
    tdb.upsert_tenant_sender(u2, sender_number=n2, sms_provider="telnyx", sms_enabled=True, registration_status="verified")
    contact = _unique_e164("696")
    _lead(u2, contact)  # u2 knows this contact, but the SMS arrives on u1's number

    result, status = txwh.handle_messaging_webhook(_inbound_payload(contact, account=n1))
    assert status == 200
    row = db.get_sms_message(result["message_id"], u1)
    assert row is not None and row["user_id"] == u1
    assert db.get_sms_message(result["message_id"], u2) is None


# Shared platform number: the tenant with the existing conversation wins, even
# though the (unique) sender row belongs to a different subscriber.
def test_shared_platform_number_routes_to_conversation_owner(two_users, monkeypatch):
    u1, u2 = two_users
    shared = _unique_e164("883")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", shared)
    # Sender rows are UNIQUE per number — only u1 holds the platform row.
    tdb.upsert_tenant_sender(u1, sender_number=shared, sms_provider="telnyx", sms_enabled=True, registration_status="verified")
    contact = _unique_e164("697")
    lead_id, _ = _lead(u2, contact)  # but u2 owns the conversation with this contact

    result, status = txwh.handle_messaging_webhook(_inbound_payload(contact, account=shared))
    assert status == 200
    assert result["lead_id"] == lead_id
    assert db.get_sms_message(result["message_id"], u2) is not None


# 16: AI failure preserves the inbound message and flags the conversation.
def test_ai_failure_preserves_inbound_and_flags_attention(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("698")
    result, _ = txwh.handle_messaging_webhook(
        _inbound_payload(contact, account, text="What's the price?")
    )
    inbound_id, lead_id = result["message_id"], result["lead_id"]

    with patch.object(
        sms_coach, "analyze_inbound_reply", side_effect=sms_coach.SmsCoachError("boom")
    ), patch.object(TelnyxSMSProvider, "send_message") as send:
        outcome = sms_ai_agent.process_inbound_ai(
            u1, lead_id, inbound_id, "What's the price?", account
        )
    assert outcome["replied"] is False
    assert outcome["reason"] == "ai_generation_failed"
    assert not send.called
    assert db.get_sms_message(inbound_id, u1) is not None  # inbound retained
    with db.get_db() as conn:
        flagged = conn.execute(
            "SELECT 1 FROM needs_attention WHERE user_id = ? AND lead_id = ? AND reason_code = ?",
            (u1, lead_id, "ai_reply_failed"),
        ).fetchone()
    assert flagged is not None


# Reliability: a crashed inbound is retryable — the retry actually processes it.
def test_failed_processing_is_reprocessed_on_retry(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("699")
    payload = _inbound_payload(contact, account)

    with patch.object(txwh.crm_db, "add_lead_activity", side_effect=RuntimeError("db down")):
        try:
            txwh.handle_messaging_webhook(payload)
            raised = False
        except RuntimeError:
            raised = True
    assert raised  # surfaces as 5xx so Telnyx retries

    # Retry of the same event id must succeed and the inbound exists exactly once.
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    pmid = payload["data"]["payload"]["id"]
    assert db.get_sms_message_by_provider_id(pmid) is not None
    with db.get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_messages WHERE provider_message_id = ?",
            (pmid,),
        ).fetchone()["c"]
    assert count == 1


# Compliance gate inside the agent: toll-free verification blocks automated sends.
def test_auto_reply_respects_toll_free_gate(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    contact = _unique_e164("701")
    result, _ = txwh.handle_messaging_webhook(_inbound_payload(contact, account))

    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch.object(TelnyxSMSProvider, "send_message") as send:
        outcome = sms_ai_agent.process_inbound_ai(
            u1, result["lead_id"], result["message_id"], "Hello", account
        )
    assert outcome["replied"] is False
    assert outcome["reason"] == "toll_free_not_verified"
    assert not send.called
