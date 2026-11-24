"""Phase III-6: stale-opportunity detection.

A lead in an open lane state is stale when (a) no contact in either
direction for 7 days, or (b) no reply from the lead within 7 days of
the last agent outbound. Detection only: the flag never sends, never
changes status/score, never creates a follow-up send or approval, and
never touches the outreach ledger.

Red-before-green: the positive tests fail on un-fixed code (the stale
reasons do not exist, filter_leads has no stale kwarg, the backdate
route does not exist). Negative guards pass on un-fixed code by
construction and are kept as regression guards.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import crm_db
import db
from migrations.runner import apply_pending_migrations


def _days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _lead(user_id, status="contacted", name="Stale Lead", **fields):
    """Create a lead, then backdate/set its contact fields directly.

    create_lead_record leaves all four last-contact fields NULL unless
    a touch flag is given, so every timestamp here is explicit.
    """
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    lead_id = db.create_lead_record(user_id, phone, name=name, status=status)
    if fields:
        with db.get_db() as conn:
            sets = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(
                f"UPDATE leads SET {sets} WHERE id = ?",
                (*fields.values(), lead_id),
            )
            conn.commit()
    return lead_id


def _open_stale_items(user_id, lead_id=None):
    with db.get_db() as conn:
        sql = """
            SELECT * FROM needs_attention
            WHERE user_id = ? AND status = 'open'
              AND reason_code IN ('stale_no_contact', 'stale_no_reply')
        """
        params = [user_id]
        if lead_id is not None:
            sql += " AND lead_id = ?"
            params.append(lead_id)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _flagged_activities(user_id, lead_id):
    return [
        a
        for a in crm_db.list_lead_activities(user_id, lead_id, limit=100)
        if a.get("event_type") == "stale_flagged"
    ]


def _table_count(table, user_id=None):
    with db.get_db() as conn:
        if user_id is None:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        else:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row["n"]


def _lead_row(lead_id):
    with db.get_db() as conn:
        return dict(
            conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        )


# --- Criterion 1: stale definition -------------------------------------------


def test_limb_a_no_contact_8_days_is_stale(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason_code"] == "stale_no_contact"
    assert items[0]["priority"] == "high"


def test_limb_b_no_reply_8_days_after_outbound_is_stale(two_users):
    u1, _ = two_users
    lead_id = _lead(
        u1,
        last_inbound_at=_days_ago(9),
        last_outbound_at=_days_ago(8),
        last_contacted_at=_days_ago(8),
        latest_call_at=None,
    )
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason_code"] == "stale_no_reply"
    assert items[0]["priority"] == "high"


def test_fresh_lead_not_stale(two_users):
    u1, _ = two_users
    recent = _days_ago(6)
    lead_id = _lead(
        u1,
        last_inbound_at=recent,
        last_outbound_at=recent,
        last_contacted_at=recent,
        latest_call_at=recent,
    )
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_never_contacted_new_lead_not_stale_under_limb_a(two_users):
    u1, _ = two_users
    # Never contacted: all contact fields NULL, created 8 days ago.
    # no_first_contact owns this lead at 24h; limb (a) must not claim it.
    lead_id = _lead(u1, status="new", created_at=_days_ago(8))
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_future_appointment_suppresses_limb_a(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        status="appointment_scheduled",
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    appt_id, err = crm_db.create_appointment(
        u1,
        {
            "lead_id": lead_id,
            "start_at": future,
            "end_at": (datetime.now(timezone.utc) + timedelta(days=2, minutes=30)).isoformat(),
            "status": "scheduled",
        },
    )
    assert err is None and appt_id is not None
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_future_appointment_does_not_suppress_limb_b(two_users):
    u1, _ = two_users
    lead_id = _lead(
        u1,
        status="appointment_scheduled",
        last_inbound_at=_days_ago(9),
        last_outbound_at=_days_ago(8),
        last_contacted_at=_days_ago(8),
        latest_call_at=None,
    )
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    appt_id, err = crm_db.create_appointment(
        u1,
        {
            "lead_id": lead_id,
            "start_at": future,
            "end_at": (datetime.now(timezone.utc) + timedelta(days=2, minutes=30)).isoformat(),
            "status": "scheduled",
        },
    )
    assert err is None and appt_id is not None
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason_code"] == "stale_no_reply"


@pytest.mark.parametrize(
    "status",
    ["closed_won", "closed_lost", "do_not_contact", "unqualified", "under_contract"],
)
def test_excluded_statuses_not_stale(two_users, status):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        status=status,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"sms_consent_status": "opted_out"},
        {"sms_consent_status": "revoked"},
        {"opt_out_status": "opted_out"},
    ],
)
def test_opted_out_or_revoked_not_stale(two_users, overrides):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
        **overrides,
    )
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_limb_b_suppressed_when_call_after_outbound(two_users):
    u1, _ = two_users
    lead_id = _lead(
        u1,
        last_inbound_at=_days_ago(9),
        last_outbound_at=_days_ago(8),
        last_contacted_at=_days_ago(2),
        latest_call_at=_days_ago(2),
    )
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_limb_b_not_suppressed_when_call_before_outbound(two_users):
    u1, _ = two_users
    lead_id = _lead(
        u1,
        last_inbound_at=_days_ago(10),
        last_outbound_at=_days_ago(8),
        last_contacted_at=_days_ago(8),
        latest_call_at=_days_ago(9),
    )
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason_code"] == "stale_no_reply"


# --- Criterion 2: flagging + clear -------------------------------------------


def test_flag_visible_via_list_and_stale_filter(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    stale_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    fresh_id = _lead(u1, name="Fresh Lead", last_inbound_at=_hours_ago(1))
    items = crm_db.list_needs_attention(u1)
    assert any(
        i.get("lead_id") == stale_id and i.get("reason_code") == "stale_no_contact"
        for i in items
    )
    stale_leads = crm_db.filter_leads(u1, stale=True)
    assert [l["id"] for l in stale_leads] == [stale_id]
    assert fresh_id not in [l["id"] for l in stale_leads]


def test_contact_clears_flag_inbound(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    assert len(_open_stale_items(u1, lead_id)) == 1
    # Simulate the inbound receipt writer: last_inbound_at = now.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET last_inbound_at = ?, last_contacted_at = ? WHERE id = ?",
            (_hours_ago(0), _hours_ago(0), lead_id),
        )
        conn.commit()
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_contact_clears_flag_outbound(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    assert len(_open_stale_items(u1, lead_id)) == 1
    # Simulate the agent's outbound: last_outbound_at = now.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET last_outbound_at = ?, last_contacted_at = ? WHERE id = ?",
            (_hours_ago(0), _hours_ago(0), lead_id),
        )
        conn.commit()
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_contact_clears_flag_call(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    assert len(_open_stale_items(u1, lead_id)) == 1
    # Simulate a voice call: latest_call_at = now.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET latest_call_at = ?, last_contacted_at = ? WHERE id = ?",
            (_hours_ago(0), _hours_ago(0), lead_id),
        )
        conn.commit()
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


def test_both_limbs_one_item_reply_precedence(two_users):
    u1, _ = two_users
    # Outbound 8d ago, inbound 9d ago: anchor is the 8d-old outbound, so
    # limb (a) holds; the outbound is unanswered, so limb (b) holds too.
    lead_id = _lead(
        u1,
        last_inbound_at=_days_ago(9),
        last_outbound_at=_days_ago(8),
        last_contacted_at=_days_ago(8),
        latest_call_at=None,
    )
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason_code"] == "stale_no_reply"


def test_reason_change_resolves_old_creates_new(two_users):
    u1, _ = two_users
    # Start stale under (b): outbound 10d ago, inbound 11d ago.
    lead_id = _lead(
        u1,
        last_inbound_at=_days_ago(11),
        last_outbound_at=_days_ago(10),
        last_contacted_at=_days_ago(10),
        latest_call_at=None,
    )
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1 and items[0]["reason_code"] == "stale_no_reply"
    # The lead replied 8d ago (after the outbound): limb (b) no longer
    # holds, but the 8d-old anchor still makes limb (a) hold.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET last_inbound_at = ?, last_contacted_at = ? WHERE id = ?",
            (_days_ago(8), _days_ago(8), lead_id),
        )
        conn.commit()
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason_code"] == "stale_no_contact"


def test_stale_item_clears_when_lead_leaves_open_lane(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    assert len(_open_stale_items(u1, lead_id)) == 1
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET status = 'closed_won' WHERE id = ?", (lead_id,)
        )
        conn.commit()
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []


# --- Criterion 3: evidence, not silence --------------------------------------


def test_first_flag_writes_one_timeline_entry(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    entries = _flagged_activities(u1, lead_id)
    assert len(entries) == 1
    summary = entries[0]["summary"]
    assert "stale_no_contact" in summary or "no contact" in summary.lower()
    assert old[:10] in summary  # last-contact date
    assert "8" in summary  # day count at flagging
    payload = crm_db.parse_activity_payload(entries[0])
    assert payload.get("reason_code") == "stale_no_contact"
    assert payload.get("days_stale") == 8


def test_recompute_writes_no_further_entries(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u1)
    for _ in range(3):
        crm_db.refresh_needs_attention(u1)
    assert len(_flagged_activities(u1, lead_id)) == 1
    assert len(_open_stale_items(u1, lead_id)) == 1


def test_reason_text_names_reason_and_date(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=_days_ago(9),
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=None,
    )
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert "No reply" in items[0]["reason_text"]
    assert old[:10] in items[0]["reason_text"]  # last-outbound date


# --- Criterion 4: tenant isolation + no LIKE ---------------------------------


def test_tenant_isolation(two_users):
    u1, u2 = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    crm_db.refresh_needs_attention(u2)
    assert _open_stale_items(u2) == []
    assert lead_id not in [l["id"] for l in crm_db.filter_leads(u2, stale=True)]
    crm_db.refresh_needs_attention(u1)
    assert len(_open_stale_items(u1, lead_id)) == 1
    assert _open_stale_items(u2) == []


def test_no_like_in_stale_sql():
    """III-2 lesson: the new stale SQL uses equality only — no LIKE.

    The stale queries live in crm_db._stale_flag_updates and in the
    STALE_LEADS_EXISTS_SQL fragment; filter_leads itself already has a
    LIKE for the free-text search box, so the assertion is scoped to
    the new stale code, not the whole module.
    """
    import inspect

    import crm_db

    source = inspect.getsource(crm_db._stale_flag_updates)
    body = source.split('"""', 2)[2] if source.count('"""') >= 2 else source
    assert "like" not in body.lower()
    assert "like" not in crm_db.STALE_LEADS_EXISTS_SQL.lower()


# --- Criterion 5/6: zero-send / zero-status-write -----------------------------


def test_flag_path_makes_zero_sends_and_zero_status_writes(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    lead_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )

    def snapshot():
        return {
            "sms": _table_count("sms_messages", u1),
            "follow_up_sends": _table_count("harness_follow_up_sends", u1),
            "approvals": _table_count("harness_approvals", u1),
            "ledger": _table_count("harness_outreach_ledger", u1),
            "lead": _lead_row(lead_id),
            "status_changes": [
                a
                for a in crm_db.list_lead_activities(u1, lead_id, limit=100)
                if a.get("event_type") == "status_change"
            ],
        }

    before = snapshot()
    crm_db.refresh_needs_attention(u1)
    # The flag path ran: one stale item + one timeline entry exist.
    assert len(_open_stale_items(u1, lead_id)) == 1
    assert len(_flagged_activities(u1, lead_id)) == 1
    after = snapshot()
    assert after["sms"] == before["sms"] == 0
    assert after["follow_up_sends"] == before["follow_up_sends"] == 0
    assert after["approvals"] == before["approvals"] == 0
    assert after["ledger"] == before["ledger"] == 0
    assert after["lead"] == before["lead"]  # byte-identical lead row
    assert after["status_changes"] == before["status_changes"] == []

    # The clear path is equally inert: contact resumes, flag resolves,
    # still zero sends and zero status writes.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET last_inbound_at = ? WHERE id = ?",
            (_hours_ago(0), lead_id),
        )
        conn.commit()
    cleared_before = snapshot()
    crm_db.refresh_needs_attention(u1)
    assert _open_stale_items(u1, lead_id) == []
    cleared_after = snapshot()
    assert cleared_after["sms"] == cleared_before["sms"] == 0
    assert cleared_after["follow_up_sends"] == cleared_before["follow_up_sends"] == 0
    assert cleared_after["approvals"] == cleared_before["approvals"] == 0
    assert cleared_after["ledger"] == cleared_before["ledger"] == 0
    assert cleared_after["status_changes"] == cleared_before["status_changes"] == []
    # The refresh itself is inert on the lead row: the only change in
    # this phase was the test's own UPDATE, which happened before the
    # cleared_before snapshot, so nothing differs here.
    changed = {
        k
        for k in cleared_after["lead"]
        if cleared_after["lead"][k] != cleared_before["lead"][k]
    }
    assert changed == set()


# --- Criterion 2/5: filters + pipeline metrics + pages -----------------------


def test_pipeline_metrics_stale_count(two_users):
    u1, _ = two_users
    old = _days_ago(8)
    _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    _lead(u1, name="Fresh Lead", last_inbound_at=_hours_ago(1))
    metrics = crm_db.get_pipeline_metrics(u1)
    assert metrics["stale"] == 1


def test_needs_attention_reason_filter_page(app_client, two_users):
    u1, _ = two_users
    old = _days_ago(8)
    stale_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    _login(app_client, u1)
    res = app_client.get("/crm/needs-attention?reason=stale_no_contact")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert f"/crm/leads/{stale_id}" in html
    res = app_client.get("/crm/needs-attention?reason=follow_up_overdue")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert f"/crm/leads/{stale_id}" not in html


def test_leads_page_stale_filter(app_client, two_users):
    u1, _ = two_users
    old = _days_ago(8)
    stale_id = _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    fresh_id = _lead(u1, name="Fresh Lead", last_inbound_at=_hours_ago(1))
    _login(app_client, u1)
    res = app_client.get("/crm/leads?stale=1")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert f"/crm/leads/{stale_id}" in html
    assert f"/crm/leads/{fresh_id}" not in html
    assert "Stale" in html


def test_dashboard_stale_card(app_client, two_users):
    u1, _ = two_users
    old = _days_ago(8)
    _lead(
        u1,
        last_inbound_at=old,
        last_outbound_at=old,
        last_contacted_at=old,
        latest_call_at=old,
    )
    _login(app_client, u1)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    res = app_client.get(f"/dashboard?local_date={day}&tz_offset_minutes=0")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Stale leads" in html
    assert 'href="/crm/leads?stale=1"' in html


# --- Backdate endpoint (ruling e, mechanism (a)) ------------------------------


def _backdate_payload(days_ago=8):
    return {"contact_at": _days_ago(days_ago)}


def test_backdate_endpoint_requires_staging(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    _login(app_client, u1)
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/test-backdate-contact",
        json=_backdate_payload(),
    )
    assert res.status_code == 404


def test_backdate_endpoint_requires_admin(app_client, two_users, monkeypatch):
    import config

    u1, _ = two_users
    lead_id = _lead(u1)
    monkeypatch.setattr(config, "IS_STAGING", True)
    monkeypatch.delenv("HARNESS_ADMIN_EMAILS", raising=False)
    _login(app_client, u1)
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/test-backdate-contact",
        json=_backdate_payload(),
    )
    assert res.status_code == 403


def test_backdate_endpoint_sets_fields_and_audit(app_client, two_users, monkeypatch):
    import config

    u1, _ = two_users
    with db.get_db() as conn:
        email = conn.execute(
            "SELECT email FROM users WHERE id = ?", (u1,)
        ).fetchone()["email"]
    monkeypatch.setattr(config, "IS_STAGING", True)
    monkeypatch.setenv("HARNESS_ADMIN_EMAILS", email)
    lead_id = _lead(u1)
    _login(app_client, u1)
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/test-backdate-contact",
        json=_backdate_payload(8),
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["lead_id"] == lead_id
    row = _lead_row(lead_id)
    day = _days_ago(8)[:10]
    assert row["last_inbound_at"].startswith(day)
    assert row["last_outbound_at"].startswith(day)
    assert row["last_contacted_at"].startswith(day)
    assert row["latest_call_at"].startswith(day)
    assert row["created_at"].startswith(day)
    with db.get_db() as conn:
        audit = conn.execute(
            """
            SELECT * FROM harness_audit
            WHERE user_id = ? AND action = 'test_backdate_contact'
              AND target_id = ?
            """,
            (u1, lead_id),
        ).fetchall()
    assert len(audit) == 1
    # End to end: the aged lead now flags as stale under limb (a).
    crm_db.refresh_needs_attention(u1)
    items = _open_stale_items(u1, lead_id)
    assert len(items) == 1
    assert items[0]["reason_code"] == "stale_no_contact"


def test_backdate_endpoint_tenant_scoped(app_client, two_users, monkeypatch):
    import config

    u1, u2 = two_users
    with db.get_db() as conn:
        email = conn.execute(
            "SELECT email FROM users WHERE id = ?", (u1,)
        ).fetchone()["email"]
    monkeypatch.setattr(config, "IS_STAGING", True)
    monkeypatch.setenv("HARNESS_ADMIN_EMAILS", email)
    other_lead = _lead(u2)
    _login(app_client, u1)
    res = app_client.post(
        f"/api/crm/leads/{other_lead}/test-backdate-contact",
        json=_backdate_payload(),
    )
    assert res.status_code == 404


def test_backdate_endpoint_rejects_bad_timestamp(app_client, two_users, monkeypatch):
    import config

    u1, _ = two_users
    with db.get_db() as conn:
        email = conn.execute(
            "SELECT email FROM users WHERE id = ?", (u1,)
        ).fetchone()["email"]
    monkeypatch.setattr(config, "IS_STAGING", True)
    monkeypatch.setenv("HARNESS_ADMIN_EMAILS", email)
    lead_id = _lead(u1)
    _login(app_client, u1)
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/test-backdate-contact",
        json={"contact_at": "not-a-date"},
    )
    assert res.status_code == 400
    res = app_client.post(
        f"/api/crm/leads/{lead_id}/test-backdate-contact", json={}
    )
    assert res.status_code == 400
