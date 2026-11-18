"""Mode-change path tests (Phase II Batch 1, item 1).

Covers the harness settings setters (``harness.policy.set_mode`` /
``set_paused``), the admin-only mode route and the self-service pause
route (``POST /api/harness/settings/mode|pause``), the audit entries
both write, and the mode control + pause toggle on the /harness page.

Admin is the ``HARNESS_ADMIN_EMAILS`` allowlist (comma-separated
emails, read at call time). Mode changes are admin-only even for the
caller's own account; pausing is self-service (own account) plus
admin for any tenant.
"""

import json

import pytest

import config
import db
from harness.policy import get_settings, is_harness_admin, set_mode, set_paused


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _email(user_id):
    return db.get_user_by_id(user_id)["email"]


def _row(user_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT mode, paused, ai_enabled FROM harness_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def _audit_actions(user_id):
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT action, actor, gate_reason FROM harness_audit "
            "WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@pytest.fixture
def _admin(two_users, monkeypatch):
    """Make the first test user a harness admin; return (admin_id, other_id)."""
    u1, u2 = two_users
    monkeypatch.setenv("HARNESS_ADMIN_EMAILS", _email(u1))
    return u1, u2


# --- is_harness_admin -------------------------------------------------------


def test_is_harness_admin_allowlist(two_users, monkeypatch):
    u1, u2 = two_users
    monkeypatch.setenv("HARNESS_ADMIN_EMAILS", _email(u1))
    assert is_harness_admin(db.get_user_by_id(u1))
    assert not is_harness_admin(db.get_user_by_id(u2))
    assert not is_harness_admin(None)
    assert not is_harness_admin({})


def test_is_harness_admin_empty_allowlist(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.delenv("HARNESS_ADMIN_EMAILS", raising=False)
    assert not is_harness_admin(db.get_user_by_id(u1))


def test_is_harness_admin_case_insensitive(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setenv("HARNESS_ADMIN_EMAILS", _email(u1).upper())
    assert is_harness_admin(db.get_user_by_id(u1))


# --- set_mode / set_paused setters ------------------------------------------


def test_set_mode_authorized_updates_row_and_audits(two_users):
    u1, _ = two_users
    settings, error = set_mode(u1, "authorized", actor="admin", actor_user_id=u1)
    assert error is None
    assert settings["mode"] == "authorized"
    assert _row(u1)["mode"] == "authorized"
    actions = _audit_actions(u1)
    assert any(a["action"] == "harness_mode_changed" for a in actions)
    assert any(
        a["action"] == "harness_mode_changed"
        and "drafts_only -> authorized" in (a["gate_reason"] or "")
        for a in actions
    )


def test_set_mode_invalid_mode_rejected_and_row_unchanged(two_users):
    u1, _ = two_users
    settings, error = set_mode(u1, "live", actor="admin", actor_user_id=u1)
    assert error == "invalid_mode"
    assert settings is None
    row = _row(u1)
    assert row is None or row["mode"] == "drafts_only"
    assert not any(
        a["action"] == "harness_mode_changed" for a in _audit_actions(u1)
    )


def test_set_mode_unknown_user_rejected(two_users):
    _, error = set_mode(999999999, "authorized", actor="admin", actor_user_id=1)
    assert error == "invalid_user"


def test_set_paused_toggles_and_audits(two_users):
    u1, _ = two_users
    settings, error = set_paused(u1, True, actor="user", actor_user_id=u1)
    assert error is None
    assert settings["paused"] is True
    assert _row(u1)["paused"] == 1
    settings, error = set_paused(u1, False, actor="user", actor_user_id=u1)
    assert error is None
    assert settings["paused"] is False
    assert _row(u1)["paused"] == 0
    actions = [a for a in _audit_actions(u1) if a["action"] == "harness_pause_changed"]
    assert len(actions) == 2
    assert "False -> True" in (actions[0]["gate_reason"] or "")
    assert "True -> False" in (actions[1]["gate_reason"] or "")


def test_set_paused_non_bool_rejected(two_users):
    u1, _ = two_users
    _, error = set_paused(u1, "yes", actor="user", actor_user_id=u1)
    assert error == "invalid_paused"
    _, error = set_paused(u1, 1, actor="user", actor_user_id=u1)
    assert error == "invalid_paused"


def test_set_mode_in_staging_stores_but_reads_drafts_only(two_users, monkeypatch):
    """The staging mode lock is untouched: a stored authorized mode in
    staging still gates as drafts_only (the setter records, the reader
    locks)."""
    u1, _ = two_users
    monkeypatch.setattr(config, "IS_STAGING", True)
    settings, error = set_mode(u1, "authorized", actor="admin", actor_user_id=u1)
    assert error is None
    assert _row(u1)["mode"] == "authorized"
    assert settings["mode"] == "drafts_only"
    assert get_settings(u1)["mode"] == "drafts_only"


# --- mode route -------------------------------------------------------------


def test_mode_route_anonymous_is_401(app_client, two_users):
    u1, _ = two_users
    resp = app_client.post("/api/harness/settings/mode", json={"mode": "authorized"})
    assert resp.status_code == 401


def test_mode_route_non_admin_forbidden_even_for_self(app_client, _admin):
    _, other = _admin
    _login(app_client, other)
    resp = app_client.post(
        "/api/harness/settings/mode", json={"mode": "authorized"}
    )
    assert resp.status_code == 403
    assert _row(other) is None or _row(other)["mode"] == "drafts_only"


def test_mode_route_admin_sets_own_mode(app_client, _admin):
    admin, _ = _admin
    _login(app_client, admin)
    resp = app_client.post(
        "/api/harness/settings/mode", json={"mode": "authorized"}
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mode"] == "authorized"
    assert data["stored_mode"] == "authorized"
    assert _row(admin)["mode"] == "authorized"


def test_mode_route_admin_sets_other_tenant(app_client, _admin):
    admin, other = _admin
    _login(app_client, admin)
    resp = app_client.post(
        "/api/harness/settings/mode",
        json={"mode": "supervised", "user_id": other},
    )
    assert resp.status_code == 200
    assert resp.get_json()["mode"] == "supervised"
    assert _row(other)["mode"] == "supervised"


def test_mode_route_invalid_mode_is_400(app_client, _admin):
    admin, _ = _admin
    _login(app_client, admin)
    resp = app_client.post("/api/harness/settings/mode", json={"mode": "live"})
    assert resp.status_code == 400


def test_mode_route_unknown_user_is_404(app_client, _admin):
    admin, _ = _admin
    _login(app_client, admin)
    resp = app_client.post(
        "/api/harness/settings/mode",
        json={"mode": "authorized", "user_id": 999999999},
    )
    assert resp.status_code == 404


# --- pause route ------------------------------------------------------------


def test_pause_route_anonymous_is_401(app_client, two_users):
    resp = app_client.post("/api/harness/settings/pause", json={"paused": True})
    assert resp.status_code == 401


def test_pause_route_tenant_pauses_self(app_client, _admin):
    _, other = _admin
    _login(app_client, other)
    resp = app_client.post("/api/harness/settings/pause", json={"paused": True})
    assert resp.status_code == 200
    assert resp.get_json()["paused"] is True
    assert _row(other)["paused"] == 1


def test_pause_route_tenant_cannot_pause_other(app_client, _admin):
    admin, other = _admin
    _login(app_client, other)
    resp = app_client.post(
        "/api/harness/settings/pause", json={"paused": True, "user_id": admin}
    )
    assert resp.status_code == 403
    assert _row(admin) is None or _row(admin)["paused"] == 0


def test_pause_route_admin_pauses_other(app_client, _admin):
    admin, other = _admin
    _login(app_client, admin)
    resp = app_client.post(
        "/api/harness/settings/pause", json={"paused": True, "user_id": other}
    )
    assert resp.status_code == 200
    assert _row(other)["paused"] == 1


def test_pause_route_non_bool_is_400(app_client, _admin):
    _, other = _admin
    _login(app_client, other)
    resp = app_client.post("/api/harness/settings/pause", json={"paused": "yes"})
    assert resp.status_code == 400


# --- /harness page controls -------------------------------------------------


def test_harness_page_shows_pause_toggle_but_no_mode_control_for_tenant(
    app_client, _admin
):
    _, other = _admin
    _login(app_client, other)
    resp = app_client.get("/harness")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<button type="button" id="harness-pause-toggle"' in html
    assert "Pause automation" in html
    # The mode <select> element must be absent for non-admins. (The page
    # script mentions the id by name, so match the element markup.)
    assert '<select id="harness-mode-select">' not in html
    assert 'id="harness-mode-save"' not in html
    assert "There are no mode controls on this page" not in html


def test_harness_page_shows_mode_control_for_admin(app_client, _admin):
    admin, _ = _admin
    _login(app_client, admin)
    resp = app_client.get("/harness")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<select id="harness-mode-select">' in html
    assert 'id="harness-mode-save"' in html
    assert "Drafts Only" in html
