"""Dashboard Pipeline metric cards → filtered destination lists."""

import re
import uuid
from datetime import datetime, timedelta, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _lead(user_id, name="Pipe Lead", status="new", **extra):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    lead_id = db.upsert_lead(
        user_id, phone, {"name": name, "lead_type": "buyer"}, source=extra.get("source", "sms")
    )
    if status and status != "new":
        crm_db.set_lead_status(user_id, lead_id, status)
    if extra.get("sms_consent_status") or extra.get("sms_sending_blocked") is not None or extra.get(
        "opt_out_status"
    ) or extra.get("consent_status"):
        with db.get_db() as conn:
            fields = []
            params = []
            if extra.get("sms_consent_status"):
                fields.append("sms_consent_status = ?")
                params.append(extra["sms_consent_status"])
            if extra.get("sms_sending_blocked") is not None:
                fields.append("sms_sending_blocked = ?")
                params.append(1 if extra["sms_sending_blocked"] else 0)
            if extra.get("opt_out_status"):
                fields.append("opt_out_status = ?")
                params.append(extra["opt_out_status"])
            if extra.get("consent_status"):
                fields.append("consent_status = ?")
                params.append(extra["consent_status"])
            if extra.get("external_source_id") is not None:
                fields.append("external_source_id = ?")
                params.append(extra["external_source_id"])
            if extra.get("source"):
                fields.append("source = ?")
                params.append(extra["source"])
            if fields:
                params.append(lead_id)
                conn.execute(
                    f"UPDATE leads SET {', '.join(fields)} WHERE id = ?",
                    params,
                )
                conn.commit()
    return lead_id


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _has_href(html, path):
    return path in html or path.replace("&", "&amp;") in html


def test_all_metric_cards_are_semantic_links(app_client, two_users):
    u1, _ = two_users
    _lead(u1, status="new")
    _login(app_client, u1)
    day = _today()
    res = app_client.get(f"/dashboard?local_date={day}&tz_offset_minutes=0")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert 'href="/crm/leads?active=1"' in html
    assert re.search(r'aria-label="View \d+ active leads"', html)
    # Zero-count cards remain clickable anchors (empty filtered view).
    assert 'href="/crm/follow-ups?due=overdue' in html
    assert 'class="metric metric-zero"' not in html
    assert html.count('class="metric metric-link"') >= 20
    # Overview usage cards
    assert 'href="/app"' in html
    assert 'href="/app#coldcall"' in html
    assert 'href="/app#voice"' in html
    assert 'href="/app#sms"' in html
    assert "stat-link" in html


def test_each_card_routes_to_correct_destination(app_client, two_users):
    u1, _ = two_users
    day = _today()
    lead_id = _lead(u1, status="qualified")
    due_today = f"{day}T15:00:00+00:00"
    crm_db.set_lead_follow_up(u1, lead_id, due_today, "Call today")
    overdue = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    lead2 = _lead(u1, name="Overdue Lead", status="contacted")
    crm_db.set_lead_follow_up(u1, lead2, overdue, "Missed call")
    crm_db.create_task(
        u1, {"title": "Task today", "due_at": f"{day}T12:00:00", "status": "open"}
    )
    crm_db.create_appointment(
        u1,
        {
            "lead_id": lead_id,
            "start_at": f"{day}T14:00:00",
            "appointment_type": "phone_call",
        },
    )
    crm_db.upsert_needs_attention(u1, lead_id, "unreviewed_inbound", priority="high")
    db.create_lead_insight(
        lead_id,
        u1,
        None,
        {
            "summary": "Wants showing",
            "intent": "schedule",
            "suggested_reply": "Happy to help — Sat or Sun?",
            "raw_json": "{}",
        },
    )

    _login(app_client, u1)
    res = app_client.get(f"/dashboard?local_date={day}&tz_offset_minutes=0")
    html = res.get_data(as_text=True)

    expected = [
        ("/crm/leads?active=1", "active leads"),
        ("/crm/needs-attention?status=open", "needing attention"),
        ("/crm/needs-attention?type=draft_reply&status=open", "drafts awaiting"),
        ("/crm/follow-ups?due=today", "follow-ups due today"),
        ("/crm/follow-ups?due=overdue", "overdue follow-ups"),
        ("/crm/follow-ups?due=this-week", "follow-ups due this week"),
        ("/crm/tasks?due=today", "tasks due today"),
        ("/crm/calendar?date=today&event_type=appointment", "appointments today"),
        ("/crm/leads?origin=external", "external leads"),
        ("/crm/leads?consent=unverified", "unverified consent"),
        ("/crm/leads?consent=review", "consent review"),
        ("/crm/leads?sms_blocked=1", "SMS blocked"),
        ("/crm/leads?consent=verified&sms_blocked=0", "verified SMS"),
        ("/crm/leads?consent=opted_out", "opted out"),
        ("/crm/leads?status=qualified", "leads in Qualified"),
        ("/crm/leads?status=contacting", "leads in Contacting"),
        ("/crm/leads?status=new", "leads in New"),
        ("/app", "listing generations"),
        ("/app#coldcall", "call script runs"),
        ("/app#voice", "AI calls"),
        ("/app#sms", "AI SMS"),
    ]
    for href, label_fragment in expected:
        assert _has_href(html, href), f"missing href {href}"
        assert label_fragment.lower() in html.lower()


def test_crm_dashboard_alias_redirects(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.get("/crm/dashboard?local_date=2026-07-30&tz_offset_minutes=0")
    assert res.status_code in {301, 302}
    assert "/dashboard" in res.headers.get("Location", "")


def test_destination_count_matches_dashboard(app_client, two_users):
    u1, _ = two_users
    day = _today()
    for i, status in enumerate(["new", "qualified", "nurture", "under_contract"]):
        _lead(u1, name=f"L{i}", status=status)

    metrics = crm_db.get_pipeline_metrics(u1, local_date=day, tz_offset_minutes=0)
    active = metrics["active_leads"]
    assert active == len(crm_db.filter_leads(u1, scope="active", limit=100000))

    _login(app_client, u1)
    res = app_client.get("/crm/leads?active=1")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Active leads" in html
    assert f"{active} result" in html
    assert "Clear filters" in html
    assert 'name="active" value="1"' in html or 'name="active"' in html

    for stage in metrics["pipeline_stages"]:
        if stage["count"] == 0:
            continue
        listed = crm_db.filter_leads(u1, stage=stage["id"], limit=100000)
        assert len(listed) == stage["count"]
        page = app_client.get(f"/crm/leads?status={stage['id']}")
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert f"{stage['count']} result" in body
        assert "Clear filters" in body
        assert f'value="{stage["id"]}"' in body and "selected" in body


def test_follow_up_task_appointment_counts_match_lists(app_client, two_users):
    u1, _ = two_users
    db.update_business_profile(u1, timezone="UTC")
    day = _today()
    lead_id = _lead(u1)
    # Later today in UTC so it remains in the this-week window (now <= due).
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    if later.strftime("%Y-%m-%d") != day:
        later = datetime.now(timezone.utc).replace(
            hour=23, minute=30, second=0, microsecond=0
        )
    crm_db.set_lead_follow_up(u1, lead_id, later.isoformat(), "Today FU")
    overdue = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    crm_db.set_lead_follow_up(
        u1, _lead(u1, name="Old"), overdue, "Overdue FU", replace_existing=False
    )
    lead_b = _lead(u1, name="Week Lead")
    week_due = (datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=2)).isoformat()
    crm_db.set_lead_follow_up(u1, lead_b, f"{week_due}T10:00:00+00:00", "Later this week")

    crm_db.create_task(
        u1, {"title": "Due today task", "due_at": f"{day}T09:00:00", "status": "open"}
    )
    crm_db.create_appointment(
        u1,
        {
            "lead_id": lead_id,
            "start_at": f"{day}T11:00:00",
            "appointment_type": "buyer_consultation",
        },
    )

    metrics = crm_db.get_pipeline_metrics(u1, timezone_name="UTC")
    today_fu = crm_db.list_follow_ups_for_dashboard_range(
        u1, "today", timezone_name="UTC"
    )
    overdue_fu = crm_db.list_follow_ups_for_dashboard_range(
        u1, "overdue", timezone_name="UTC"
    )
    week_fu = crm_db.list_follow_ups_for_dashboard_range(
        u1, "this_week", timezone_name="UTC"
    )
    assert metrics["follow_ups_due_today"] == len(today_fu)
    assert metrics["overdue_follow_ups"] == len(overdue_fu)
    assert metrics["follow_ups_due_this_week"] == len(week_fu)
    assert metrics["tasks_due_today"] == crm_db.count_tasks_due_today(u1, local_date=day)
    assert metrics["appointments_today"] == crm_db.count_appointments_today(
        u1, local_date=day
    )

    _login(app_client, u1)
    for path, count in [
        ("/crm/follow-ups?due=today", metrics["follow_ups_due_today"]),
        ("/crm/follow-ups?due=overdue", metrics["overdue_follow_ups"]),
        ("/crm/follow-ups?due=this-week", metrics["follow_ups_due_this_week"]),
        (f"/crm/tasks?due=today&local_date={day}", metrics["tasks_due_today"]),
        (f"/crm/calendar?date=today&event_type=appointment&local_date={day}", metrics["appointments_today"]),
    ]:
        res = app_client.get(path)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "Clear filters" in html
        assert f"{count} result" in html


def test_consent_and_sms_filters_match_dashboard_counts(app_client, two_users):
    u1, u2 = two_users
    day = _today()
    _lead(u1, name="Unverified", sms_consent_status="unverified", sms_sending_blocked=True)
    _lead(
        u1,
        name="Not Certified",
        sms_consent_status="not_certified",
        sms_sending_blocked=True,
    )
    _lead(
        u1,
        name="Verified",
        sms_consent_status="verified",
        sms_sending_blocked=False,
    )
    _lead(
        u1,
        name="User Certified",
        sms_consent_status="user_certified",
        sms_sending_blocked=False,
    )
    _lead(
        u1,
        name="Legacy Confirmed",
        sms_consent_status="not_certified",
        sms_sending_blocked=False,
        consent_status="confirmed",
    )
    _lead(u1, name="Opted Consent", sms_consent_status="opted_out")
    _lead(u1, name="Opted Flag", opt_out_status="opted_out", sms_consent_status="unverified")
    _lead(
        u1,
        name="External",
        source="external:zillow",
        external_source_id=1,
        sms_consent_status="unverified",
    )
    lead_review = _lead(u1, name="Review Me", sms_consent_status="unverified")
    crm_db.upsert_needs_attention(
        u1, lead_review, "consent_review_required", priority="high"
    )
    # Cross-tenant noise
    _lead(u2, name="Other Verified", sms_consent_status="verified", sms_sending_blocked=False)
    _lead(
        u2,
        name="Other Certified",
        sms_consent_status="user_certified",
        sms_sending_blocked=False,
    )

    metrics = crm_db.get_pipeline_metrics(u1, local_date=day, tz_offset_minutes=0)
    # Current CRM rows use not_certified / user_certified; legacy unverified / verified
    # must still count on the same Pipeline cards.
    unverified_names = {
        lead["name"]
        for lead in crm_db.filter_leads(u1, sms_consent_status="unverified")
    }
    verified_names = {
        lead["name"]
        for lead in crm_db.filter_leads(
            u1, sms_consent_status="verified", sms_sending_blocked=False
        )
    }
    assert "Not Certified" in unverified_names
    assert "Unverified" in unverified_names
    assert "User Certified" not in unverified_names
    assert "User Certified" in verified_names
    assert "Verified" in verified_names
    assert "Legacy Confirmed" in verified_names
    assert "Not Certified" not in verified_names
    assert "Legacy Confirmed" not in unverified_names
    assert metrics["unverified_consent"] == len(unverified_names)
    assert metrics["verified_consent"] == 3
    _login(app_client, u1)

    cases = [
        ("/crm/leads?consent=unverified", metrics["unverified_consent"], "Unverified"),
        ("/crm/leads?consent=verified&sms_blocked=0", metrics["verified_consent"], "Verified"),
        ("/crm/leads?consent=opted_out", metrics["opted_out_consent"], "Opted"),
        ("/crm/leads?sms_blocked=1", metrics["sms_blocked"], "blocked"),
        ("/crm/leads?origin=external", metrics["external_leads"], "External"),
        ("/crm/leads?consent=review", metrics["consent_review_required"], "Review Me"),
    ]
    for path, count, fragment in cases:
        listed_count = None
        if "consent=unverified" in path and "sms_blocked" not in path and "review" not in path:
            listed_count = crm_db.count_filtered_leads(u1, sms_consent_status="unverified")
        elif "verified" in path:
            listed_count = crm_db.count_filtered_leads(
                u1, sms_consent_status="verified", sms_sending_blocked=False
            )
        elif "opted_out" in path:
            listed_count = crm_db.count_filtered_leads(u1, sms_consent_status="opted_out")
        elif "sms_blocked=1" in path:
            listed_count = crm_db.count_filtered_leads(u1, sms_sending_blocked=True)
        elif "origin=external" in path:
            listed_count = crm_db.count_filtered_leads(u1, external_only=True)
        elif "consent=review" in path:
            listed_count = crm_db.count_filtered_leads(u1, consent_review_required=True)
        assert listed_count == count, f"{path}: dashboard {count} vs filter {listed_count}"

        res = app_client.get(path)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert f"{count} result" in html
        assert "Clear filters" in html
        assert "Other Verified" not in html
        assert fragment in html or fragment.lower() in html.lower()
        if "consent=verified" in path:
            assert "Legacy Confirmed" in html
            assert "User Certified" in html
            assert "Consent: Verified" in html
            assert "Not Certified" not in html


def test_needs_attention_count_matches_destination(app_client, two_users):
    u1, _ = two_users
    day = _today()
    for i in range(3):
        lid = _lead(u1, name=f"NA{i}")
        crm_db.upsert_needs_attention(u1, lid, "unreviewed_inbound", priority="high")
    metrics = crm_db.get_pipeline_metrics(u1, local_date=day, tz_offset_minutes=0)
    _login(app_client, u1)
    res = app_client.get(f"/crm/needs-attention?status=open&local_date={day}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert f"{metrics['needs_attention']} result" in html
    assert metrics["needs_attention"] >= 3


def test_zero_count_cards_open_empty_state(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    html = app_client.get(
        f"/dashboard?local_date={day}&tz_offset_minutes=0"
    ).get_data(as_text=True)
    assert _has_href(html, "/crm/follow-ups?due=overdue")
    res = app_client.get(
        f"/crm/follow-ups?due=overdue&local_date={day}&tz_offset_minutes=0"
    )
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "0 result" in body or "Clear filters" in body
    assert "Overdue follow-ups" in body


def test_unknown_query_params_safely_ignored(app_client, two_users):
    u1, _ = two_users
    _lead(u1, name="Keep Me", status="qualified")
    _login(app_client, u1)
    res = app_client.get(
        "/crm/leads?status=not_a_real_status&consent=bogus&origin=mars&sms_blocked=maybe&stage=nope"
    )
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    # Unknown filters ignored → unfiltered tenant list still loads
    assert "Keep Me" in html
    assert "Clear filters" not in html or "Active leads" not in html


def test_filters_enforced_server_side_and_cross_tenant(app_client, two_users):
    u1, u2 = two_users
    day = _today()
    _lead(u1, name="Mine New", status="new")
    _lead(u1, name="Mine Qual", status="qualified")
    other = _lead(u2, name="Other Qual", status="qualified")

    _login(app_client, u1)
    res = app_client.get("/crm/leads?status=qualified")
    html = res.get_data(as_text=True)
    assert "Mine Qual" in html
    assert "Other Qual" not in html
    assert "Mine New" not in html

    ids = {l["id"] for l in crm_db.filter_leads(u1, stage="qualified")}
    assert other not in ids

    metrics_u1 = crm_db.get_pipeline_metrics(u1, local_date=day)
    metrics_u2 = crm_db.get_pipeline_metrics(u2, local_date=day)
    stage_u1 = next(s for s in metrics_u1["pipeline_stages"] if s["id"] == "qualified")
    stage_u2 = next(s for s in metrics_u2["pipeline_stages"] if s["id"] == "qualified")
    assert stage_u1["count"] == 1
    assert stage_u2["count"] == 1


def test_keyboard_focus_and_accessible_labels(app_client, two_users):
    u1, _ = two_users
    _lead(u1, status="new")
    _login(app_client, u1)
    day = _today()
    html = app_client.get(
        f"/dashboard?local_date={day}&tz_offset_minutes=0"
    ).get_data(as_text=True)

    assert "metric-link:focus-visible" in html
    assert "stat-link:focus-visible" in html
    assert 'class="metric metric-link"' in html
    assert re.search(r'aria-label="View \d+ [^"]+"', html)
    assert "cursor: pointer" in html
    # Whole card is one anchor — no nested interactive elements inside metric links
    metric_links = re.findall(
        r'<a class="metric metric-link"[^>]*>.*?</a>', html, flags=re.S
    )
    assert metric_links
    for block in metric_links:
        assert "<a " not in block[3:]  # no nested anchors
        assert "<button" not in block


def test_legacy_filter_params_still_work(app_client, two_users):
    u1, _ = two_users
    _lead(u1, status="qualified")
    _login(app_client, u1)
    for path in (
        "/crm/leads?scope=active",
        "/crm/leads?stage=qualified",
        "/crm/leads?sms_consent=unverified",
        "/crm/leads?external=1",
        "/crm/leads?blocked=1",
        "/crm/follow-ups?range=overdue&status=open",
        "/crm/tasks?range=today&status=open",
    ):
        res = app_client.get(path)
        assert res.status_code == 200, path


def test_mobile_metrics_grid_styles_present(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get(
        f"/dashboard?local_date={_today()}&tz_offset_minutes=0"
    ).get_data(as_text=True)
    assert "@media (max-width: 900px)" in html
    assert "grid-template-columns: 1fr 1fr" in html
