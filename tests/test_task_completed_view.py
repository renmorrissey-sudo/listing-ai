"""Completed-task viewing, filtering, and reopen behavior on the Tasks page."""

import uuid
from datetime import datetime, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _lead(user_id, name="Task Lead"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    return db.upsert_lead(user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms")


def _make_task(user_id, title, lead_id=None, due_at=None, priority="normal", task_type="general_follow_up"):
    task_id, err = crm_db.create_task(
        user_id,
        {
            "title": title,
            "lead_id": lead_id,
            "due_at": due_at,
            "priority": priority,
            "task_type": task_type,
        },
    )
    assert err is None, err
    return task_id


# --- Backend model ------------------------------------------------------------

def test_complete_sets_completed_at_and_by(two_users):
    u1, _ = two_users
    tid = _make_task(u1, "Call seller")
    task, err = crm_db.complete_task(u1, tid, actor_user_id=u1)
    assert err is None
    assert task["status"] == "completed"
    assert task["completed_at"]
    assert task["completed_by"] == u1


def test_complete_is_idempotent_no_duplicate_activity(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    tid = _make_task(u1, "Send CMA", lead_id=lead_id)
    first, _ = crm_db.complete_task(u1, tid, actor_user_id=u1)
    first_completed_at = first["completed_at"]
    # Repeated clicks must not change completion metadata or add activity rows.
    crm_db.complete_task(u1, tid, actor_user_id=u1)
    crm_db.complete_task(u1, tid, actor_user_id=u1)
    again = crm_db.get_task(u1, tid)
    assert again["completed_at"] == first_completed_at
    activities = crm_db.list_lead_activities(u1, lead_id)
    completed_events = [a for a in activities if a["event_type"] == "task_completed"]
    assert len(completed_events) == 1


def test_completed_task_removed_from_open_buckets(two_users):
    u1, _ = two_users
    day = _today()
    tid = _make_task(u1, "Due today task", due_at=f"{day}T12:00:00+00:00")
    assert any(t["id"] == tid for t in crm_db.list_tasks(u1, "today", local_date=day))
    crm_db.complete_task(u1, tid, actor_user_id=u1)
    for bucket in ("today", "overdue", "upcoming"):
        assert all(t["id"] != tid for t in crm_db.list_tasks(u1, bucket, local_date=day))


def test_reopen_returns_to_open_and_clears_completion(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    day = _today()
    tid = _make_task(u1, "Reopen me", lead_id=lead_id, due_at=f"{day}T12:00:00+00:00")
    crm_db.complete_task(u1, tid, actor_user_id=u1)
    task, err = crm_db.reopen_task(u1, tid, actor_user_id=u1)
    assert err is None
    assert task["status"] == "open"
    assert task["completed_at"] is None
    assert task["completed_by"] is None
    # Returns to the open bucket.
    assert any(t["id"] == tid for t in crm_db.list_tasks(u1, "today", local_date=day))
    # Auditable history recorded.
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "task_reopened" for a in activities)


def test_reopen_idempotent_on_open_task(two_users):
    u1, _ = two_users
    lead_id = _lead(u1)
    tid = _make_task(u1, "Already open", lead_id=lead_id)
    task, err = crm_db.reopen_task(u1, tid, actor_user_id=u1)
    assert err is None and task["status"] == "open"
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert not any(a["event_type"] == "task_reopened" for a in activities)


def test_list_completed_tasks_filters(two_users):
    u1, _ = two_users
    lead_a = _lead(u1, "Lead A")
    lead_b = _lead(u1, "Lead B")
    t_high = _make_task(u1, "High call", lead_id=lead_a, priority="high", task_type="call")
    t_low = _make_task(u1, "Low email", lead_id=lead_b, priority="low", task_type="send_email")
    crm_db.complete_task(u1, t_high, actor_user_id=u1)
    crm_db.complete_task(u1, t_low, actor_user_id=u1)

    all_completed = crm_db.list_completed_tasks(u1, completion_range="all")
    assert {t["id"] for t in all_completed} == {t_high, t_low}

    by_priority = crm_db.list_completed_tasks(u1, completion_range="all", priority="high")
    assert {t["id"] for t in by_priority} == {t_high}

    by_type = crm_db.list_completed_tasks(u1, completion_range="all", task_type="send_email")
    assert {t["id"] for t in by_type} == {t_low}

    by_lead = crm_db.list_completed_tasks(u1, completion_range="all", lead_id=lead_a)
    assert {t["id"] for t in by_lead} == {t_high}

    # A custom range in the far past excludes tasks completed today.
    past = crm_db.list_completed_tasks(
        u1, completion_range="custom", start_date="2000-01-01", end_date="2000-01-02"
    )
    assert past == []


# --- HTTP / rendering ---------------------------------------------------------

def test_open_view_hides_completed_completed_view_shows_it(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    tid = _make_task(u1, "Visible then hidden", due_at=f"{day}T12:00:00+00:00")

    open_html = app_client.get(f"/crm/tasks?status=open&local_date={day}").get_data(as_text=True)
    assert "Visible then hidden" in open_html

    app_client.post(f"/api/crm/tasks/{tid}/complete")

    open_html2 = app_client.get(f"/crm/tasks?status=open&local_date={day}").get_data(as_text=True)
    assert "Visible then hidden" not in open_html2

    done_html = app_client.get(f"/crm/tasks?status=completed&local_date={day}").get_data(as_text=True)
    assert "Visible then hidden" in done_html
    assert "Completed tasks" in done_html


def test_all_view_shows_open_and_completed(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    open_id = _make_task(u1, "Still open task", due_at=f"{day}T12:00:00+00:00")
    done_id = _make_task(u1, "Finished task", due_at=f"{day}T13:00:00+00:00")
    app_client.post(f"/api/crm/tasks/{done_id}/complete")

    html = app_client.get(f"/crm/tasks?status=all&local_date={day}").get_data(as_text=True)
    assert "Still open task" in html
    assert "Finished task" in html
    assert open_id and done_id


def test_completed_view_shows_details_and_timestamp(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    lead_id = _lead(u1, "Detail Lead")
    tid = _make_task(
        u1, "Detailed task", lead_id=lead_id, due_at=f"{day}T09:00:00+00:00", priority="high"
    )
    # Add notes via update.
    crm_db.update_task(u1, tid, {"title": "Detailed task", "description": "Bring comps", "priority": "high"})
    app_client.post(f"/api/crm/tasks/{tid}/complete")

    html = app_client.get(f"/crm/tasks?status=completed&local_date={day}").get_data(as_text=True)
    assert "Detailed task" in html
    assert "Detail Lead" in html          # related lead
    assert "Bring comps" in html          # notes
    assert "high" in html                 # priority
    assert day in html                    # completion timestamp (date portion) shown
    assert "completed-title" in html      # strikethrough style applied


def test_completed_task_can_be_reopened_via_api(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    tid = _make_task(u1, "Roundtrip task", due_at=f"{day}T12:00:00+00:00")
    app_client.post(f"/api/crm/tasks/{tid}/complete")
    res = app_client.post(f"/api/crm/tasks/{tid}/reopen")
    assert res.status_code == 200
    assert res.get_json()["task"]["status"] == "open"
    open_html = app_client.get(f"/crm/tasks?status=open&local_date={day}").get_data(as_text=True)
    assert "Roundtrip task" in open_html


def test_dashboard_open_counts_exclude_completed(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    t1 = _make_task(u1, "Open due today", due_at=f"{day}T12:00:00+00:00")
    t2 = _make_task(u1, "Completed due today", due_at=f"{day}T13:00:00+00:00")
    assert crm_db.count_tasks_due_today(u1, local_date=day) == 2
    app_client.post(f"/api/crm/tasks/{t2}/complete")
    assert crm_db.count_tasks_due_today(u1, local_date=day) == 1
    metrics = crm_db.get_pipeline_metrics(u1, local_date=day, tz_offset_minutes=0)
    assert metrics["tasks_due_today"] == 1
    assert metrics["tasks_completed_today"] == 1
    assert t1


def test_completed_filters_are_shareable_by_url(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    keep = _make_task(u1, "High priority done", priority="high", task_type="call")
    other = _make_task(u1, "Low priority done", priority="low", task_type="send_email")
    app_client.post(f"/api/crm/tasks/{keep}/complete")
    app_client.post(f"/api/crm/tasks/{other}/complete")

    url = f"/crm/tasks?status=completed&completion=all&priority=high&local_date={day}"
    html = app_client.get(url).get_data(as_text=True)
    assert "High priority done" in html
    assert "Low priority done" not in html
    # The rendered filter form reflects the URL params so browser Back restores it.
    assert '<option value="high" selected' in html


def test_tenant_isolation_for_completed_and_reopen(app_client, two_users):
    u1, u2 = two_users
    day = _today()
    tid = _make_task(u1, "U1 secret task", due_at=f"{day}T12:00:00+00:00")
    crm_db.complete_task(u1, tid, actor_user_id=u1)

    # u2 cannot see u1's completed task.
    assert crm_db.list_completed_tasks(u2, completion_range="all") == []
    _login(app_client, u2)
    html = app_client.get(f"/crm/tasks?status=completed&local_date={day}").get_data(as_text=True)
    assert "U1 secret task" not in html

    # u2 cannot reopen or complete u1's task.
    assert app_client.post(f"/api/crm/tasks/{tid}/reopen").status_code == 404
    assert app_client.post(f"/api/crm/tasks/{tid}/complete").status_code == 404
    # u1's task remains completed and untouched.
    assert crm_db.get_task(u1, tid)["status"] == "completed"


def test_status_filter_validates_and_defaults_to_open(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    _make_task(u1, "Default open task", due_at=f"{day}T12:00:00+00:00")
    # Garbage status must fall back to the Open view (no error, open content shown).
    res = app_client.get(f"/crm/tasks?status=' OR 1=1 --&local_date={day}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Default open task" in html
    assert 'aria-current="page"' in html  # a valid tab is marked active


def test_mobile_and_desktop_layout_markers_present(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    day = _today()
    tid = _make_task(u1, "Layout task", due_at=f"{day}T12:00:00+00:00")
    app_client.post(f"/api/crm/tasks/{tid}/complete")
    html = app_client.get(f"/crm/tasks?status=all&local_date={day}").get_data(as_text=True)
    # Responsive viewport + horizontally scrollable tables for small screens.
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    assert "table-scroll" in html
    assert 'class="status-tabs"' in html
