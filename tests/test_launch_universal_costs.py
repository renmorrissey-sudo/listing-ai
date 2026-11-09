"""Universal AI cost recording + spend-cap enforcement (launch blocker 5).

One record test and one cap-block test per billable path (see
docs/ai-cost-coverage.md for the inventory), plus ledger-level tests
for the placeholder rates and idempotent true-ups. Every provider call
is mocked; no test places a real call or uses a real provider key.

RELEASE VARIANT (topai-launch-readiness-release, 2026-11-09): this
release contains no schema migrations, so migration 033 (model /
correlation_id columns on harness_costs) is excluded. ``record_cost``
already degrades to the 029 column set, so every record test below
asserts the cost row is still recorded (provider/tool/units/cost_cents)
and that the model/correlation_id keys are absent, and every cap-block
test still asserts the model is never called when the cap is tripped.
Idempotent true-up dedup is lost without the correlation_id column
(the redelivery tests below pin that degraded behaviour explicitly);
over-counting trips the cap sooner, so the degradation fails safe.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
import db
import tenant_sms_db as tdb
from harness import costs
from tests.test_telnyx_inbound_ai_workflow import (
    _analysis,
    _inbound_payload,
    _lead,
    _setup_tenant,
    _unique_e164,
)

CAP_ENV = "HARNESS_MONTHLY_SPEND_CAP_CENTS"


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _trip_cap(monkeypatch, user_id, spend_cents=5000):
    """Seed spend above a tiny cap so the gate trips for *user_id*."""
    monkeypatch.setenv(CAP_ENV, "100")
    costs.record_cost(
        user_id, "anthropic", "seed", 1000, "tokens", cost_cents=spend_cents
    )
    assert costs.is_spend_cap_tripped(user_id)


def _rows(user_id, tool):
    return [r for r in costs.list_costs(user_id) if r["tool"] == tool]


def _fake_anthropic_message(text="ok", *, input_tokens=1000, output_tokens=500):
    block = SimpleNamespace(text=text)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
    )


def _persona_id(user_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM voice_personas WHERE user_id IS NULL OR user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# Ledger: degraded 033 behaviour (release variant — migration 033
# excluded, so model/correlation_id are dropped at write time),
# zero-token skip, placeholder rates, Telnyx-reported cost, idempotent
# true-ups (dedup lost without the correlation_id column — pinned
# explicitly below), cap status.
# ---------------------------------------------------------------------------


def test_record_cost_records_without_model_columns_on_pre033_schema(two_users):
    """record_cost still records the spend on the 029 column set.

    Release variant: migration 033 is excluded, so model and
    correlation_id are silently dropped by _insert_cost_row's fallback;
    provider/tool/units/cost_cents are still recorded exactly.
    """
    u1, _ = two_users
    row_id = costs.record_cost(
        u1, "anthropic", "ask_topai", 1500, "tokens", cost_cents=5,
        model="claude-sonnet-5", correlation_id="req-abc",
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert "model" not in row
    assert "correlation_id" not in row
    assert row["provider"] == "anthropic"
    assert row["tool"] == "ask_topai"
    assert row["units"] == 1500
    assert row["cost_cents"] == 5


def test_record_anthropic_usage_skips_zero_token_rows(two_users):
    u1, _ = two_users
    assert costs.record_anthropic_usage(u1, "ask_topai", {}) is None
    assert costs.record_anthropic_usage(u1, "ask_topai", None) is None
    assert _rows(u1, "ask_topai") == []


def test_placeholder_rates_defaults_and_env_override(monkeypatch):
    assert costs.get_vapi_price_cents_per_minute() == 10
    assert costs.get_openai_realtime_price_cents_per_minute() == 6
    assert costs.get_sendgrid_price_cents_per_message() == 1
    assert costs.get_sms_price_cents_per_unit("telnyx") == 1
    assert costs.get_sms_price_cents_per_unit("twilio") == 1
    assert costs.get_sms_price_cents_per_unit("simpletexting") == 2
    assert costs.get_sms_price_cents_per_unit("mock") == 0
    monkeypatch.setenv("HARNESS_VAPI_PRICE_CENTS_PER_MINUTE", "25")
    assert costs.get_vapi_price_cents_per_minute() == 25
    monkeypatch.setenv("HARNESS_SMS_PRICE_CENTS_PER_TELNYX_PER_UNIT", "3")
    assert costs.get_sms_price_cents_per_unit("telnyx") == 3


def test_record_sms_send_uses_telnyx_reported_cost(two_users):
    u1, _ = two_users
    row_id = costs.record_sms_send(
        u1, provider="telnyx", tool="sms_send",
        result={"provider_message_id": "tx-1", "segments": 2,
                "cost": {"amount": "0.05"}},
        correlation_id="corr-1",
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["units"] == 2
    assert row["unit_type"] == "segments"
    assert row["cost_cents"] == 5  # provider-reported $0.05, not the placeholder
    assert "correlation_id" not in row  # release: 033 excluded


def test_record_sms_send_falls_back_to_placeholder_and_mock_is_free(two_users):
    u1, _ = two_users
    row_id = costs.record_sms_send(
        u1, provider="telnyx", tool="sms_send", result={}, correlation_id="c1"
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["units"] == 1
    assert row["cost_cents"] == 1  # 1 segment x 1 cent placeholder
    row_id = costs.record_sms_send(
        u1, provider="mock", tool="sms_send", result={}, correlation_id="c2"
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["cost_cents"] == 0


def test_vapi_duration_trueup_no_dedup_on_pre033_schema(two_users):
    """Vapi true-up records on the 029 column set; dedup is lost.

    Release variant: without migration 033 there is no correlation_id
    column, so record_cost_once cannot detect a redelivery and the
    second true-up inserts a second row. This is pinned explicitly:
    it over-counts (trips the cap sooner), so it fails safe, and the
    dedup behaviour returns when 033 lands in a future release.
    """
    u1, _ = two_users
    row_id = costs.record_vapi_call_duration(
        u1, duration_seconds=150, correlation_id="vapi-1"
    )
    assert row_id is not None
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["tool"] == "vapi_call_duration"
    assert row["units"] == 2  # ceil(150/60)=3 min total, minus 1 min at start
    assert row["cost_cents"] == 20  # 2 min x 10 cent placeholder
    # Redelivery is NOT deduped on the pre-033 schema: a second row.
    assert costs.record_vapi_call_duration(
        u1, duration_seconds=150, correlation_id="vapi-1"
    ) is not None
    assert len(_rows(u1, "vapi_call_duration")) == 2
    # Calls of 60s or less need no true-up.
    assert costs.record_vapi_call_duration(
        u1, duration_seconds=60, correlation_id="vapi-2"
    ) is None


def test_spend_cap_status_and_check(two_users, monkeypatch):
    u1, _ = two_users
    allowed, reason = costs.check_spend_cap(u1)
    assert allowed is True and reason is None
    _trip_cap(monkeypatch, u1)
    status = costs.spend_cap_status(u1)
    assert status["cap_cents"] == 100
    assert status["spent_cents"] == 5000
    assert status["tripped"] is True
    allowed, reason = costs.check_spend_cap(u1)
    assert allowed is False
    assert "spend limit" in reason


def test_record_cost_once_no_dedup_on_pre033_schema(two_users):
    """record_cost_once cannot dedupe without the correlation_id column.

    Release variant: on the pre-033 schema the dedup lookup has no
    column to query, so a second identical call inserts a second row.
    Pinned explicitly — over-counting fails safe (cap trips sooner).
    """
    u1, _ = two_users
    first = costs.record_cost_once(
        u1, "vapi", "vapi_call_duration", 2, "minutes",
        cost_cents=20, correlation_id="dup-1",
    )
    assert first is not None
    assert costs.record_cost_once(
        u1, "vapi", "vapi_call_duration", 2, "minutes",
        cost_cents=20, correlation_id="dup-1",
    ) is not None
    assert len(_rows(u1, "vapi_call_duration")) == 2


# ---------------------------------------------------------------------------
# Paths 1-3: listing / script / SMS generation routes (app.py).
# ---------------------------------------------------------------------------

LISTING_PAYLOAD = {
    "address": "123 Main Street, Denver, CO",
    "price": "485,000",
    "beds": "4",
    "baths": "2.5",
    "sqft": "2,200",
    "year_built": "",
    "garage": "None",
    "pool": "No",
    "features": "",
    "neighborhood": "",
}


def _listing_message():
    raw = (
        "---LISTING DESCRIPTION---\nA great home.\n"
        "---SOCIAL POSTS---\n1. [INSTAGRAM] hi\n"
        "---PROSPECT EMAIL---\nSubject: Hi\n\nBody."
    )
    return _fake_anthropic_message(raw, input_tokens=1000, output_tokens=500)


def test_generate_listing_records_cost(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    with patch("app.client.messages.create", return_value=_listing_message()):
        res = app_client.post("/generate", json=LISTING_PAYLOAD)
    assert res.status_code == 200, res.get_json()
    rows = _rows(u1, "listing_generation")
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "anthropic"
    assert "model" not in row  # release: 033 excluded
    assert row["units"] == 1500  # exact token count
    # Opus estimate: (1000*1500 + 500*7500) / 1e6 = 5.25 -> 5 cents
    assert row["cost_cents"] == 5
    assert "correlation_id" not in row  # release: 033 excluded


def test_generate_listing_blocked_when_cap_tripped(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    _trip_cap(monkeypatch, u1)
    with patch(
        "app.client.messages.create", side_effect=AssertionError("model must not be called")
    ):
        res = app_client.post("/generate", json=LISTING_PAYLOAD)
    assert res.status_code == 402
    assert "spend limit" in res.get_json()["error"]
    assert _rows(u1, "listing_generation") == []


def _script_message():
    raw = (
        "---OPENING SCRIPT---\nHello.\n---OBJECTION HANDLERS---\nHandle it.\n"
        "---VOICEMAIL SCRIPT---\nCall me back."
    )
    return _fake_anthropic_message(raw, input_tokens=1000, output_tokens=1000)


def test_generate_script_records_cost(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    with patch("app.client.messages.create", return_value=_script_message()):
        res = app_client.post(
            "/generate-script", json={"area": "Testville", "name": "Pat"}
        )
    assert res.status_code == 200, res.get_json()
    rows = _rows(u1, "script_generation")
    assert len(rows) == 1
    assert "model" not in rows[0]  # release: 033 excluded
    assert rows[0]["units"] == 2000
    # Sonnet estimate: (1000*300 + 1000*1500) / 1e6 = 1.8 -> 2 cents
    assert rows[0]["cost_cents"] == 2
    assert "correlation_id" not in rows[0]  # release: 033 excluded


def test_generate_script_blocked_when_cap_tripped(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    _trip_cap(monkeypatch, u1)
    with patch(
        "app.client.messages.create", side_effect=AssertionError("model must not be called")
    ):
        res = app_client.post(
            "/generate-script", json={"area": "Testville", "name": "Pat"}
        )
    assert res.status_code == 402
    assert _rows(u1, "script_generation") == []


def test_generate_sms_records_cost(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    with patch(
        "app.client.messages.create",
        return_value=_fake_anthropic_message("Hi there!", input_tokens=100, output_tokens=50),
    ):
        res = app_client.post(
            "/sms/generate",
            json={
                "persona_id": _persona_id(u1),
                "phone_number": "+13038703107",
                "lead_name": "Pat",
            },
        )
    assert res.status_code == 200, res.get_json()
    rows = _rows(u1, "sms_generation")
    assert len(rows) == 1
    assert "model" not in rows[0]  # release: 033 excluded
    assert rows[0]["units"] == 150
    assert "correlation_id" not in rows[0]  # release: 033 excluded


def test_generate_sms_blocked_when_cap_tripped(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    _trip_cap(monkeypatch, u1)
    with patch(
        "app.client.messages.create", side_effect=AssertionError("model must not be called")
    ):
        res = app_client.post(
            "/sms/generate",
            json={
                "persona_id": _persona_id(u1),
                "phone_number": "+13038703107",
                "lead_name": "Pat",
            },
        )
    assert res.status_code == 402
    assert _rows(u1, "sms_generation") == []

# ---------------------------------------------------------------------------
# Path 4/5: SMS coach (direct + harness paths) via analyze_inbound_and_coach.
# ---------------------------------------------------------------------------

COACH_JSON = json.dumps(
    {
        "summary": "Lead wants a showing this weekend",
        "intent": "schedule showing",
        "recommended_next_action": "Propose two showing times",
        "draft_reply": "Happy to help - does Sat 11am or Sun 2pm work?",
        "confidence": 0.82,
        "sensitive_topic": False,
        "suggested_lead_status": "qualified",
        "suggested_follow_up_at": None,
        "suggested_follow_up_reason": "",
        "suggested_tasks": [],
        "appointment_requested": False,
        "appointment_details": None,
        "needs_attention_reasons": [],
        "home_value_pitch": "",
    }
)


class _FakeAnthropicClient:
    """Anthropic stand-in returning a fixed coach reply + usage."""

    def __init__(self, *args, **kwargs):
        self.messages = self

    def create(self, **kwargs):
        return _fake_anthropic_message(
            COACH_JSON, input_tokens=800, output_tokens=200
        )


def _inbound_lead(two_users, monkeypatch):
    import telnyx_webhooks as txwh

    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("651")
    lead_id, _ = _lead(u1, contact)
    result, status = txwh.handle_messaging_webhook(
        _inbound_payload(contact, account, text="Tell me more")
    )
    assert status == 200
    return u1, account, contact, lead_id, result["message_id"]


def test_sms_coach_records_cost(two_users, monkeypatch):
    import sms_coach
    from sms_inbound import analyze_inbound_and_coach

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    monkeypatch.setattr(sms_coach, "Anthropic", _FakeAnthropicClient)
    coach = analyze_inbound_and_coach(u1, lead_id, inbound_id, "Tell me more")
    assert coach.get("error") is None, coach
    assert coach.get("analysis")
    rows = _rows(u1, "sms_coach")
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "anthropic"
    assert "model" not in row  # release: 033 excluded
    assert row["units"] == 1000  # exact token count
    assert row["cost_cents"] == 3  # (800*1500 + 200*7500) / 1e6 = 2.7 -> 3


def test_sms_coach_blocked_when_cap_tripped(two_users, monkeypatch):
    import sms_coach
    from sms_inbound import analyze_inbound_and_coach

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    _trip_cap(monkeypatch, u1)

    def _boom(*args, **kwargs):
        raise AssertionError("coach model must not be called when capped")

    monkeypatch.setattr(sms_coach, "Anthropic", _boom)
    coach = analyze_inbound_and_coach(u1, lead_id, inbound_id, "Tell me more")
    assert coach.get("error") == "spend_cap_reached"
    assert coach.get("analysis") is None
    assert _rows(u1, "sms_coach") == []
    # Flagged for manual review, not silently dropped.
    assert coach.get("insight_id")


def test_harness_sms_job_blocks_on_spend_cap_without_double_count(
    two_users, monkeypatch
):
    """Harness handler: cap trip -> BLOCK, no send, no duplicate cost row."""
    from harness import sms_handler

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    _trip_cap(monkeypatch, u1)

    def _capped_coach(*args, **kwargs):
        return {"analysis": None, "insight_id": None, "error": "spend_cap_reached"}

    monkeypatch.setattr(
        "sms_inbound.analyze_inbound_and_coach", _capped_coach
    )
    # The handler imports analyze_inbound_and_coach lazily from sms_inbound.
    job = {
        "id": 1,
        "user_id": u1,
        "correlation_id": "job-cap-1",
        "payload": {"lead_id": lead_id, "inbound_id": inbound_id, "body": "Tell me more"},
    }
    with patch(
        "sms_ai_agent.process_inbound_ai",
        side_effect=AssertionError("must not send when capped"),
    ):
        result = sms_handler.process_sms_auto_reply_job(job)
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "spend_cap_reached"
    assert result["sent"] is False
    assert result["retryable"] is False
    assert _rows(u1, "sms_auto_reply") == []  # no duplicate recording


# ---------------------------------------------------------------------------
# Path 6: Ask TopAI tool-use loop (ask_topai/agent.py).
# ---------------------------------------------------------------------------


class _FakeAgentResponse:
    def __init__(self, text="Done.", *, input_tokens=2000, output_tokens=500):
        self.stop_reason = "end_turn"
        self.content = [{"type": "text", "text": text}]
        self.usage = SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        )


def test_ask_topai_records_cost_per_round(two_users, monkeypatch):
    from ask_topai import agent

    u1, _ = two_users
    monkeypatch.setattr(
        "ask_topai.agent.call_claude",
        lambda messages, *, system, tools_spec: _FakeAgentResponse(),
    )
    result = agent.complete(u1, "How many open leads do I have?", {})
    assert result["status"] in {"informational", "needs_clarification", "ok"}
    rows = _rows(u1, "ask_topai")
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "anthropic"
    assert "model" not in row  # release: 033 excluded
    assert row["units"] == 2500
    # Sonnet estimate: (2000*300 + 500*1500) / 1e6 = 1.35 -> 1 cent
    assert row["cost_cents"] == 1


def test_ask_topai_blocked_when_cap_tripped(two_users, monkeypatch):
    from ask_topai import agent

    u1, _ = two_users
    _trip_cap(monkeypatch, u1)
    monkeypatch.setattr(
        "ask_topai.agent.call_claude",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    result = agent.complete(u1, "How many open leads do I have?", {})
    assert result["status"] == "error"
    assert result["code"] == "spend_cap_reached"
    assert _rows(u1, "ask_topai") == []


# ---------------------------------------------------------------------------
# Path 7: Ask TopAI legacy parser (ask_topai/parser.py).
# ---------------------------------------------------------------------------


def test_parser_call_llm_records_cost(two_users, monkeypatch):
    import config as _config
    from ask_topai import parser

    u1, _ = two_users
    monkeypatch.setattr(_config, "ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(parser, "Anthropic", _FakeAnthropicClient)

    # The fake coach client returns coach JSON; the parser only needs
    # valid JSON out of message.content[0].text, so reuse a minimal one.
    class _ParserClient(_FakeAnthropicClient):
        def create(self, **kwargs):
            return _fake_anthropic_message(
                '{"status": "informational", "message": "ok", "commands": []}',
                input_tokens=400,
                output_tokens=100,
            )

    monkeypatch.setattr(parser, "Anthropic", _ParserClient)
    payload = parser.call_llm("hello", {}, user_id=u1, correlation_id="parse-1")
    assert payload["status"] == "informational"
    rows = _rows(u1, "ask_topai_legacy")
    assert len(rows) == 1
    assert "model" not in rows[0]  # release: 033 excluded
    assert rows[0]["units"] == 500
    assert "correlation_id" not in rows[0]  # release: 033 excluded


def test_parser_call_llm_blocked_when_cap_tripped(two_users, monkeypatch):
    import config as _config
    from ask_topai import parser

    u1, _ = two_users
    monkeypatch.setattr(_config, "ANTHROPIC_API_KEY", "sk-test-key")
    _trip_cap(monkeypatch, u1)
    monkeypatch.setattr(
        parser, "Anthropic",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    with pytest.raises(RuntimeError, match="spend limit"):
        parser.call_llm("hello", {}, user_id=u1)
    assert _rows(u1, "ask_topai_legacy") == []


# ---------------------------------------------------------------------------
# Paths 8-9: OpenAI Realtime session mint (live + diagnostics).
# ---------------------------------------------------------------------------


def test_realtime_session_records_cost(app_client, two_users, monkeypatch):
    from tests.test_ask_topai_webrtc import _install_openai, _openai_ready

    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    _openai_ready(monkeypatch)
    _install_openai(monkeypatch, [])
    res = app_client.post("/api/ask-topai/live/session", json={"context": {}})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["ok"] is True
    rows = _rows(u1, "realtime_session")
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "openai"
    assert "model" not in row  # release: 033 excluded
    assert row["units"] == 1
    assert row["unit_type"] == "sessions"
    assert row["cost_cents"] == 6  # 1-min minimum x 6 cent placeholder
    assert "correlation_id" not in row  # release: 033 excluded


def test_realtime_session_blocked_when_cap_tripped(app_client, two_users, monkeypatch):
    from tests.test_ask_topai_webrtc import _install_openai, _openai_ready

    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    _trip_cap(monkeypatch, u1)
    _openai_ready(monkeypatch)

    def _boom(req, timeout=None):
        raise AssertionError("OpenAI must not be called when capped")

    monkeypatch.setattr(
        "ask_topai.realtime.openai_client.urllib.request.urlopen", _boom
    )
    res = app_client.post("/api/ask-topai/live/session", json={"context": {}})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is False
    assert data["code"] == "spend_cap_reached"
    assert _rows(u1, "realtime_session") == []


def test_realtime_diagnostics_mint_records_cost(two_users, monkeypatch):
    from ask_topai.realtime import openai_client, service, settings

    u1, _ = two_users
    monkeypatch.setattr(settings, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "key_present", lambda: True)
    monkeypatch.setattr(
        openai_client,
        "probe_openai_auth",
        lambda **k: {
            "openai_status": 200,
            "openai_authenticated": True,
            "model_listed": "gpt-realtime-2.1",
            "message": None,
            "request_id": "req-1",
        },
    )
    minted = {"value": "ek_test123", "expires_at": 1700000000, "openai_status": 200}
    monkeypatch.setattr(
        openai_client, "mint_ephemeral_secret", lambda *a, **k: minted
    )
    body = service.diagnostics(user_id=u1, probe_secret=True)
    assert body["ok"] is True
    assert body["client_secret_created"] is True
    assert len(_rows(u1, "realtime_session")) == 1


def test_realtime_diagnostics_mint_blocked_when_cap_tripped(two_users, monkeypatch):
    from ask_topai.realtime import openai_client, service, settings

    u1, _ = two_users
    monkeypatch.setattr(settings, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "key_present", lambda: True)
    monkeypatch.setattr(
        openai_client,
        "probe_openai_auth",
        lambda **k: {
            "openai_status": 200,
            "openai_authenticated": True,
            "model_listed": "gpt-realtime-2.1",
            "message": None,
            "request_id": "req-1",
        },
    )
    _trip_cap(monkeypatch, u1)
    monkeypatch.setattr(
        openai_client,
        "mint_ephemeral_secret",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("mint must not run")),
    )
    body = service.diagnostics(user_id=u1, probe_secret=True)
    assert body["ok"] is False
    assert "spend limit" in (body["message"] or "")
    assert _rows(u1, "realtime_session") == []

# ---------------------------------------------------------------------------
# Paths 10-11: Vapi outbound call + duration true-up (app.py).
# ---------------------------------------------------------------------------


def _voice_persona(user_id):
    return db.create_voice_persona(
        user_id,
        {
            "name": "ISA",
            "persona_type": "buyer",
            "prompt": "Qualify the lead.",
            "tone": "professional",
            "goal": "Book a consult",
            "objection_handling_notes": "",
        },
    )


def _voice_payload(persona_id):
    return {
        "persona_id": persona_id,
        "lead_name": "Ben Miller",
        "phone_number": "3038703107",
        "call_purpose": "Open house follow-up",
        "lead_context": "Attended Meadow Ranch",
        "property_interest": "Townhome",
        "desired_outcome": "Book consultation",
        "compliance_confirmed": True,
    }


def _mock_vapi(monkeypatch, provider_call_id="vapi_call_1"):
    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {
                "start_outbound_call": staticmethod(
                    lambda *a, **k: {"provider_call_id": provider_call_id}
                )
            },
        )(),
    )


def test_vapi_call_records_cost(app_client, two_users, monkeypatch):
    from migrations.runner import apply_pending_migrations

    u1, _ = two_users
    apply_pending_migrations()
    db.update_business_profile(
        u1, agent_name="Ada", brokerage_name="Ada Realty", company_name="Ada Homes"
    )
    persona_id = _voice_persona(u1)
    _mock_vapi(monkeypatch, "vapi_cost_1")
    _login(app_client, u1)
    res = app_client.post("/voice/calls", json=_voice_payload(persona_id))
    assert res.status_code == 201, res.get_json()
    rows = _rows(u1, "vapi_outbound_call")
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "vapi"
    assert row["units"] == 1  # 1-minute minimum at start
    assert row["unit_type"] == "minutes"
    assert row["cost_cents"] == 10  # 1 min x 10 cent placeholder
    assert "correlation_id" not in row  # release: 033 excluded


def test_vapi_call_blocked_when_cap_tripped(app_client, two_users, monkeypatch):
    from migrations.runner import apply_pending_migrations

    u1, _ = two_users
    apply_pending_migrations()
    db.update_business_profile(
        u1, agent_name="Ada", brokerage_name="Ada Realty", company_name="Ada Homes"
    )
    persona_id = _voice_persona(u1)
    _trip_cap(monkeypatch, u1)
    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: (_ for _ in ()).throw(AssertionError("Vapi must not be called")),
    )
    _login(app_client, u1)
    res = app_client.post("/voice/calls", json=_voice_payload(persona_id))
    assert res.status_code == 402
    assert _rows(u1, "vapi_outbound_call") == []


def test_vapi_duration_trueup_on_webhook(app_client, two_users, monkeypatch):
    from migrations.runner import apply_pending_migrations

    u1, _ = two_users
    apply_pending_migrations()
    db.update_business_profile(
        u1, agent_name="Ada", brokerage_name="Ada Realty", company_name="Ada Homes"
    )
    persona_id = _voice_persona(u1)
    _mock_vapi(monkeypatch, "vapi_true_1")
    _login(app_client, u1)
    started = app_client.post("/voice/calls", json=_voice_payload(persona_id))
    assert started.status_code == 201, started.get_json()
    assert len(_rows(u1, "vapi_outbound_call")) == 1
    res = app_client.post(
        "/webhook/voice",
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "vapi_true_1", "durationSeconds": 150},
                "summary": "Lead wants a consultation",
                "endedReason": "customer-ended-call",
            }
        },
    )
    assert res.status_code == 200
    rows = _rows(u1, "vapi_call_duration")
    assert len(rows) == 1
    assert rows[0]["units"] == 2  # ceil(150/60)=3 min total, minus 1 min at start
    assert rows[0]["cost_cents"] == 20
    # Release variant: redelivery is NOT deduped on the pre-033 schema
    # (no correlation_id column), so the second webhook inserts a
    # second row. Pinned explicitly — over-counting fails safe.
    res = app_client.post(
        "/webhook/voice",
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "vapi_true_1", "durationSeconds": 150},
                "summary": "Lead wants a consultation",
                "endedReason": "customer-ended-call",
            }
        },
    )
    assert res.status_code == 200
    assert len(_rows(u1, "vapi_call_duration")) == 2


# ---------------------------------------------------------------------------
# Paths 12-16: outbound SMS sends (one-to-one, AI reply, scheduled, test).
# ---------------------------------------------------------------------------


def _telnyx_ready(monkeypatch):
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


def _telnyx_fake_request(captured):
    from sms_providers.telnyx import TelnyxSMSProvider

    def fake_request(self, method, path, body=None):
        captured["called"] = True
        return {
            "data": {
                "id": "msg_cost_1",
                "status": "queued",
                "to": [{"status": "queued"}],
                "cost": {"amount": "0.05"},
            }
        }, 200

    return patch.object(TelnyxSMSProvider, "_request", fake_request)


def test_send_authorized_sms_records_cost(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)
    captured = {}
    with _telnyx_fake_request(captured):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona_id(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201, res.get_json()
    assert captured.get("called")
    rows = _rows(u1, "sms_send")
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "telnyx"
    assert row["units"] == 1
    assert row["cost_cents"] == 5  # Telnyx-reported $0.05
    assert "correlation_id" not in row  # release: 033 excluded


def test_send_authorized_sms_blocked_when_cap_tripped(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)
    _trip_cap(monkeypatch, u1)
    captured = {}
    with _telnyx_fake_request(captured):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona_id(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 402
    assert captured.get("called") is None  # provider never called
    assert _rows(u1, "sms_send") == []


def test_ai_auto_reply_records_sms_cost(two_users, monkeypatch):
    import sms_ai_agent

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    with _patch_coach_and_send(monkeypatch):
        outcome = sms_ai_agent.process_inbound_ai(
            u1, lead_id, inbound_id, "Tell me more", account
        )
    assert outcome["replied"] is True
    rows = _rows(u1, "sms_ai_reply")
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "telnyx"
    assert row["cost_cents"] == 5  # Telnyx-reported $0.05
    assert "correlation_id" not in row  # release: 033 excluded


def _patch_coach_and_send(monkeypatch):
    """Patch the coach to return a canned analysis and Telnyx to accept sends."""
    import sms_coach
    from sms_providers.telnyx import TelnyxSMSProvider

    class _Ctx:
        def __enter__(self):
            self._p1 = patch.object(
                sms_coach, "analyze_inbound_reply", return_value=_analysis()
            )
            self._p2 = patch.object(TelnyxSMSProvider, "send_message")
            self._m1 = self._p1.__enter__()
            self._m2 = self._p2.__enter__()
            self._m2.return_value = {
                "provider_message_id": "tx-ai-cost-1",
                "status": "queued",
                "cost": {"amount": "0.05"},
            }
            return self

        def __exit__(self, *exc):
            self._p2.__exit__(*exc)
            self._p1.__exit__(*exc)
            return False

    return _Ctx()


def test_ai_auto_reply_blocked_when_cap_tripped(two_users, monkeypatch):
    import sms_ai_agent
    from sms_providers.telnyx import TelnyxSMSProvider

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    _trip_cap(monkeypatch, u1)
    with patch.object(
        TelnyxSMSProvider, "send_message",
        side_effect=AssertionError("provider must not be called when capped"),
    ):
        outcome = sms_ai_agent.process_inbound_ai(
            u1, lead_id, inbound_id, "Tell me more", account
        )
    assert outcome["replied"] is False
    assert outcome["reason"] == "spend_cap_reached"
    assert _rows(u1, "sms_ai_reply") == []


# ---------------------------------------------------------------------------
# Path 14/15: campaign worker sends (scheduled AI reply + bulk campaign).
# ---------------------------------------------------------------------------


def _scheduled_row(u1, lead_id, contact):
    message_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": "Test Lead",
            "phone_number": contact,
            "message_body": "Deferred AI reply",
        },
        status="scheduled",
        lead_id=lead_id,
        direction="outbound",
        consent_status="confirmed",
        opt_out_status="active",
    )
    return {
        "id": message_id,
        "user_id": u1,
        "lead_id": lead_id,
        "phone_number": contact,
        "message_body": "Deferred AI reply",
    }


def test_worker_scheduled_ai_reply_records_cost(two_users, monkeypatch):
    from workers import sms_campaign_worker as worker

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    row = _scheduled_row(u1, lead_id, contact)
    monkeypatch.setattr(
        worker, "get_sms_provider",
        lambda: SimpleNamespace(
            send_sms=lambda *a, **k: {
                "provider_message_id": "tx-sched-1",
                "status": "queued",
                "cost": {"amount": "0.05"},
            }
        ),
    )
    worker._send_scheduled_ai_reply(row)
    rows = _rows(u1, "sms_ai_reply")
    assert len(rows) == 1
    assert rows[0]["cost_cents"] == 5
    assert "correlation_id" not in rows[0]  # release: 033 excluded


def test_worker_scheduled_ai_reply_blocked_when_cap_tripped(two_users, monkeypatch):
    from workers import sms_campaign_worker as worker

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    _trip_cap(monkeypatch, u1)
    row = _scheduled_row(u1, lead_id, contact)
    monkeypatch.setattr(
        worker, "get_sms_provider",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )
    worker._send_scheduled_ai_reply(row)
    assert _rows(u1, "sms_ai_reply") == []
    stored = db.get_sms_message(row["id"], u1)
    assert stored["status"] == "failed"


def _campaign_fixture(u1, monkeypatch, contact, lead_id):
    cid = tdb.create_campaign(
        u1, "Cost test",
        message_template="Hi {first_name}, still interested?",
        campaign_purpose="campaign",
    )
    tdb.replace_campaign_recipients(
        cid, u1, [{"phone_number": contact, "lead_id": lead_id,
                   "merge_fields": {"first_name": "Pat"}}],
    )
    tdb.create_jobs_for_campaign(cid, u1)
    tdb.update_campaign(cid, u1, status="processing")
    return cid


def test_worker_campaign_job_records_cost(two_users, monkeypatch):
    from workers import sms_campaign_worker as worker

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    _campaign_fixture(u1, monkeypatch, contact, lead_id)
    monkeypatch.setattr(worker, "can_send_sms", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        worker, "get_sms_provider",
        lambda: SimpleNamespace(
            send_sms=lambda *a, **k: {
                "provider_message_id": "tx-camp-1",
                "status": "queued",
                "cost": {"amount": "0.05"},
            }
        ),
    )
    assert worker.process_one("w-cost-1") is True
    rows = _rows(u1, "sms_campaign")
    assert len(rows) == 1
    assert rows[0]["cost_cents"] == 5
    assert "correlation_id" not in rows[0]  # release: 033 excluded


def test_worker_campaign_job_blocked_when_cap_tripped(two_users, monkeypatch):
    from workers import sms_campaign_worker as worker

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    cid = _campaign_fixture(u1, monkeypatch, contact, lead_id)
    _trip_cap(monkeypatch, u1)
    monkeypatch.setattr(worker, "can_send_sms", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        worker, "get_sms_provider",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )
    assert worker.process_one("w-cost-2") is True
    assert _rows(u1, "sms_campaign") == []
    with db.get_db() as conn:
        job = conn.execute(
            "SELECT status FROM sms_campaign_jobs WHERE campaign_id = ?",
            (cid,),
        ).fetchone()
    assert job["status"] == "failed_permanent"


# ---------------------------------------------------------------------------
# Path 17: SendGrid lead email. Path 18: HELP compliance reply.
# ---------------------------------------------------------------------------


def test_send_lead_email_records_cost(two_users, monkeypatch):
    import email_marketing_db
    import lead_email_service
    from lead_service import upsert_crm_lead

    u1, _ = two_users
    contact = _unique_e164("651")
    lead_id, _, _ = upsert_crm_lead(
        u1, contact,
        {"name": "Em Lead", "phone_number": contact, "email": "lead@example.com"},
        source="test", assigned_user_id=u1,
    )
    monkeypatch.setattr(
        email_marketing_db, "get_credentials",
        lambda *a, **k: {
            "api_key": "sg-test", "sender_name": "Ada", "sender_email": "ada@example.com",
        },
    )

    class _FakeSendGrid:
        def __init__(self, api_key):
            pass

        def send_email(self, **kwargs):
            return {"provider_message_id": "sg-1", "provider_status": "sent"}

    monkeypatch.setattr(
        lead_email_service, "SendGridEmailCampaignProvider", _FakeSendGrid
    )
    result, err = lead_email_service.send_lead_email(
        u1, lead_id, subject="Hi", body="Hello there"
    )
    assert err is None, err
    assert result["ok"] is True
    rows = _rows(u1, "lead_email")
    assert len(rows) == 1
    assert rows[0]["provider"] == "sendgrid"
    assert rows[0]["units"] == 1
    assert rows[0]["cost_cents"] == 1  # 1 message x 1 cent placeholder
    assert "correlation_id" not in rows[0]  # release: 033 excluded


def test_send_lead_email_blocked_when_cap_tripped(two_users, monkeypatch):
    import email_marketing_db
    import lead_email_service
    from lead_service import upsert_crm_lead

    u1, _ = two_users
    contact = _unique_e164("651")
    lead_id, _, _ = upsert_crm_lead(
        u1, contact,
        {"name": "Em Lead", "phone_number": contact, "email": "lead@example.com"},
        source="test", assigned_user_id=u1,
    )
    monkeypatch.setattr(
        email_marketing_db, "get_credentials",
        lambda *a, **k: {
            "api_key": "sg-test", "sender_name": "Ada", "sender_email": "ada@example.com",
        },
    )
    _trip_cap(monkeypatch, u1)
    monkeypatch.setattr(
        lead_email_service, "SendGridEmailCampaignProvider",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("SendGrid must not be called")),
    )
    result, err = lead_email_service.send_lead_email(
        u1, lead_id, subject="Hi", body="Hello there"
    )
    assert result is None
    assert err and "spend limit" in err
    assert _rows(u1, "lead_email") == []


def test_help_reply_records_cost_but_is_not_cap_blocked(two_users, monkeypatch):
    """E1: compliance HELP replies still go out when capped — but are recorded."""
    import telnyx_webhooks as txwh

    u1, account, contact, lead_id, inbound_id = _inbound_lead(two_users, monkeypatch)
    _trip_cap(monkeypatch, u1)

    class _FakeProvider:
        def send_message(self, **kwargs):
            return {"provider_message_id": "tx-help-1", "status": "queued"}

    txwh._send_help_reply(_FakeProvider(), u1, lead_id, contact, account)
    rows = _rows(u1, "sms_help_reply")
    assert len(rows) == 1
    assert rows[0]["provider"] == "telnyx"
    assert rows[0]["cost_cents"] == 1
