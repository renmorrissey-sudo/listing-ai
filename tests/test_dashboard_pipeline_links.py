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


def _lead(user_id, name="Pipe Lead", status="new"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    lead_id = db.upsert_lead(
        user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms"
    )
    if status and status != "new":
        crm_db.set_lead_status(user_id, lead_id, status)
    return lead_id


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_nonzero_cards_are_links_zero_are_not(app_client, two_users):
    u1, _ = two_users
    _lead(u1, status="new")
    _login(app_client, u1)
    day = _today()
    res = app_client.get(f"/dashboard?local_date={day}&tz_offset_minutes=0")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # Active leads > 0 → link with accessible label
    assert 'href="/crm/leads?scope=active"' in html
    assert 'aria-label="View 1 active leads"' in html or re.search(
        r'aria-label="View \d+ active leads"', html
    )

    # Zero metric cards are not anchors
    assert 'class="metric metric-zero"' in html
    # Overdue follow-ups should be zero for a fresh lead with no follow-ups
    overdue_block = re.search(
        r'(<a[^>]*>|<div[^>]*>)\s*<div class="k">Overdue follow-ups</div>',
        html,
        re.I,
    )
    assert overdue_block
    assert overdue_block.group(0).startswith("<div")
    assert 'href="/crm/follow-ups?range=overdue' not in overdue_block.group(0)


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

    def has_href(path):
        # Jinja escapes & → &amp; in attributes.
        return path in html or path.replace("&", "&amp;") in html

    expected = [
        ("/crm/leads?scope=active", "active leads"),
        ("/crm/needs-attention?status=open", "needs attention"),
        ("/crm/needs-attention?type=draft_reply&status=open", "drafts awaiting"),
        ("/crm/follow-ups?range=today&status=open", "follow-ups due today"),
        ("/crm/follow-ups?range=overdue&status=open", "overdue follow-ups"),
        ("/crm/follow-ups?range=this_week&status=open", "follow-ups due this week"),
        ("/crm/tasks?range=today&status=open", "tasks due today"),
        ("/crm/calendar?event_type=appointment&range=today", "appointments today"),
        ("/crm/leads?stage=qualified", "leads in Qualified"),
        ("/crm/leads?stage=contacting", "leads in Contacting"),
    ]
    for href, label_fragment in expected:
        assert has_href(href), f"missing href {href}"
        assert label_fragment.lower() in html.lower()


def test_destination_count_matches_dashboard(app_client, two_users):
    u1, _ = two_users
    day = _today()
    for i, status in enumerate(["new", "qualified", "nurture", "under_contract"]):
        _lead(u1, name=f"L{i}", status=status)

    metrics = crm_db.get_pipeline_metrics(u1, local_date=day, tz_offset_minutes=0)
    active = metrics["active_leads"]
    assert active == len(crm_db.filter_leads(u1, scope="active", limit=100000))

    _login(app_client, u1)
    res = app_client.get("/crm/leads?scope=active")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Active leads" in html
    assert f"{active} result" in html
    assert "Clear filters" in html

    for stage in metrics["pipeline_stages"]:
        if stage["count"] == 0:
            continue
        listed = crm_db.filter_leads(u1, stage=stage["id"], limit=100000)
        assert len(listed) == stage["count"]
        page = app_client.get(f"/crm/leads?stage={stage['id']}")
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert f"{stage['count']} result" in body
        assert "Clear filters" in body


def test_follow_up_task_appointment_counts_match_lists(app_client, two_users):
    u1, _ = two_users
    day = _today()
    lead_id = _lead(u1)
    crm_db.set_lead_follow_up(u1, lead_id, f"{day}T16:00:00+00:00", "Today FU")
    overdue = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    crm_db.set_lead_follow_up(
        u1, _lead(u1, name="Old"), overdue, "Overdue FU", replace_existing=False
    )
    # Second open follow-up on a different lead (set_lead_follow_up may replace per lead)
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

    metrics = crm_db.get_pipeline_metrics(u1, local_date=day, tz_offset_minutes=0)
    today_fu = crm_db.list_follow_ups_for_dashboard_range(
        u1, "today", local_date=day, tz_offset_minutes=0
    )
    overdue_fu = crm_db.list_follow_ups_for_dashboard_range(
        u1, "overdue", local_date=day, tz_offset_minutes=0
    )
    week_fu = crm_db.list_follow_ups_for_dashboard_range(
        u1, "this_week", local_date=day, tz_offset_minutes=0
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
        (f"/crm/follow-ups?range=today&status=open&local_date={day}&tz_offset_minutes=0", metrics["follow_ups_due_today"]),
        (f"/crm/follow-ups?range=overdue&status=open&local_date={day}&tz_offset_minutes=0", metrics["overdue_follow_ups"]),
        (f"/crm/follow-ups?range=this_week&status=open&local_date={day}&tz_offset_minutes=0", metrics["follow_ups_due_this_week"]),
        (f"/crm/tasks?range=today&status=open&local_date={day}", metrics["tasks_due_today"]),
        (f"/crm/calendar?event_type=appointment&range=today&local_date={day}", metrics["appointments_today"]),
    ]:
        res = app_client.get(path)
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "Clear filters" in html
        assert f"{count} result" in html


def test_filters_enforced_server_side_and_cross_tenant(app_client, two_users):
    u1, u2 = two_users
    day = _today()
    _lead(u1, name="Mine New", status="new")
    _lead(u1, name="Mine Qual", status="qualified")
    other = _lead(u2, name="Other Qual", status="qualified")

    _login(app_client, u1)
    res = app_client.get("/crm/leads?stage=qualified")
    html = res.get_data(as_text=True)
    assert "Mine Qual" in html
    assert "Other Qual" not in html
    assert "Mine New" not in html

    # Direct DB list for u1 never includes u2 lead
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

    assert "a.metric.metric-link:focus-visible" in html or "metric-link:focus-visible" in html
    assert 'class="metric metric-link"' in html
    assert re.search(r'aria-label="View \d+ [^"]+"', html)
    # Zero cards keep default cursor styling via metric-zero
    assert "metric-zero" in html
    assert "cursor: pointer" in html
