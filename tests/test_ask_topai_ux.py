"""Ask TopAI UX: End Conversation cleanup and hidden lead-source labels."""

from __future__ import annotations

import json
import re
from pathlib import Path

import db
from ask_topai import audit
from external_leads.ingest import ingest_external_lead
from tests.test_ask_topai import _as_complete, _login

WIDGET = Path(__file__).resolve().parents[1] / "templates" / "ask_topai_widget.html"


def _widget():
    return WIDGET.read_text(encoding="utf-8")


def test_end_conversation_uses_shared_cleanup_and_closes_panel():
    html = _widget()
    assert "function cleanupLiveSession" in html
    assert "function teardownPeer" in html
    assert "function stopMicTracks" in html
    assert "function stopPlaybackAudio" in html
    assert "function closeDataChannels" in html
    assert "function closePeerConnection" in html
    assert "endBtn.addEventListener('click', () => cleanupLiveSession({ notify: true, closePanel: true }))" in html
    assert "ask-topai-close" in html
    assert "setOpen(false)" in html
    assert "if (hasLiveResources()) {\n      cleanupLiveSession({ notify: true, closePanel: true })" in html
    assert "endLive(!!live.sessionId)" in html
    assert "applyPanelOpen(false)" in html
    assert "fab.hidden = false" in html
    assert 'id="ask-topai-fab"' in html
    assert "live.realtime = null" in html
    assert "live.sessionId = null" in html
    assert "if (session.close) session.close()" in html
    assert "pc.close()" in html
    assert "track.stop()" in html
    assert "el.pause()" in html
    assert "beforeunload" in html
    assert "beacon: true" in html
    assert "await cleanupLiveSession({ notify: false, closePanel: false })" in html
    assert "endBtn.addEventListener('click', () => endLive(true))" not in html
    assert "else if (live.active) endLive(true)" not in html


def test_end_conversation_does_not_hide_panel_before_teardown():
    html = _widget()
    cleanup_idx = html.index("function cleanupLiveSession")
    teardown_idx = html.index("teardownPeer();", cleanup_idx)
    mic_idx = html.index("stopMicTracks();", cleanup_idx)
    finish_idx = html.index("function finishPanel()", cleanup_idx)
    assert teardown_idx < finish_idx
    assert mic_idx < finish_idx
    close_call = html.index("applyPanelOpen(false)", finish_idx)
    assert close_call > teardown_idx
    assert close_call > mic_idx


def test_clicking_x_reuses_the_same_cleanup_as_end_conversation():
    html = _widget()
    assert "document.getElementById('ask-topai-close').addEventListener('click', () => setOpen(false))" in html
    assert "if (hasLiveResources()) {\n      cleanupLiveSession({ notify: true, closePanel: true })" in html
    assert html.count("cleanupLiveSession({ notify: true, closePanel: true })") >= 2


def test_widget_still_present_on_authenticated_pages(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert 'id="ask-topai-fab"' in html
    assert "Ask TopAI" in html
    assert "cleanupLiveSession" in html
    assert "End Conversation" in html


def _create_ask_topai_lead(app_client, user_id, monkeypatch, name="John Smith", phone="303-555-1212"):
    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {
                        "name": name,
                        "phone": phone,
                        "lead_type": "buyer",
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    res = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": f"Create a buyer lead named {name} at {phone}"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "executed"
    return data


def test_ask_topai_lead_hides_internal_source_in_user_facing_ui(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _create_ask_topai_lead(app_client, u1, monkeypatch)

    leads = [lead for lead in db.list_leads(u1, limit=50) if lead.get("name") == "John Smith"]
    assert len(leads) == 1
    lead = leads[0]
    assert lead["source"] == "external:ask_topai"
    assert lead["user_id"] == u1

    list_html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "John Smith" in list_html
    assert "external:ask_topai" not in list_html
    assert "ask_topai" in list_html  # launcher/widget still present

    detail_html = app_client.get(f"/crm/leads/{lead['id']}").get_data(as_text=True)
    assert "John Smith" in detail_html
    assert "external:ask_topai" not in detail_html
    assert '<span class="pill">External source</span>' not in detail_html

    source_select = re.search(r'<select id="nl-source".*?</select>', list_html, re.S)
    assert source_select is not None
    assert "external:ask_topai" not in source_select.group(0)
    assert "ask_topai" not in source_select.group(0).lower()

    sms_payload = app_client.get("/sms/leads").get_json()
    assert "external:ask_topai" not in json.dumps(sms_payload)
    assert any(item.get("name") == "John Smith" for item in sms_payload["leads"])

    dash = app_client.get("/dashboard?local_date=2026-08-19&tz_offset_minutes=0").get_data(as_text=True)
    assert "external:ask_topai" not in dash
    assert "John Smith" in dash

    api = app_client.get("/api/crm/leads").get_json()
    match = next(item for item in api["leads"] if item["id"] == lead["id"])
    assert match["source"] == "external:ask_topai"

    rows = audit.list_recent(u1)
    assert rows
    assert rows[0]["source"] == "ask_topai"
    assert rows[0]["status"] == "executed"


def test_external_zillow_source_label_still_displays(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    result = ingest_external_lead(
        u1,
        {"full_name": "Zed Zillow", "phone": "+15551239991", "source": "zillow"},
        method="webhook",
    )
    assert result["action"] == "created"
    stored = db.get_lead(result["lead_id"], u1)
    assert stored["source"] == "external:zillow"

    list_html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "Zed Zillow" in list_html
    assert "external:zillow" in list_html

    detail_html = app_client.get(f"/crm/leads/{result['lead_id']}").get_data(as_text=True)
    assert "external:zillow" in detail_html
    assert '<span class="pill">External source</span>' in detail_html
