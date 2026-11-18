"""SMS suppression unification + governed removal (Phase II Batch 1, item 5).

* Twilio inbound STOP now writes ``sms_suppression_list`` like Telnyx
  and SimpleTexting do (previously it only set the consent flag), so
  the harness gate blocks the number on every provider.
* Suppression removal is governed, not self-service: a tenant requests
  removal with a written reason, a harness admin reviews it, and the
  reviewer must differ from the requester (two-person rule). Every
  step is written to the SMS audit trail. START never clears
  suppression — that rule is unchanged and covered by the existing
  provider tests.
"""

import uuid

import pytest

import config
import db
import tenant_sms_db as tdb


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _email(user_id):
    return (db.get_user_by_id(user_id) or {}).get("email") or ""


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _seed_lead(user_id, phone):
    return db.upsert_lead(
        user_id, phone, {"name": "Stop Lead", "lead_type": "buyer"}, source="sms"
    )


def _sms_audit_actions(user_id):
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT action FROM sms_audit_events WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [r["action"] for r in rows]


# --- Twilio STOP writes the shared suppression list --------------------------


def test_twilio_stop_writes_suppression(two_users, monkeypatch):
    from sms_inbound import parse_inbound_form, process_inbound_sms

    u1, _ = two_users
    phone = _unique_phone()
    _seed_lead(u1, phone)
    monkeypatch.setattr(config, "TWILIO_PHONE_NUMBER", "+18888210810")
    result = process_inbound_sms(
        parse_inbound_form(
            {
                "From": phone,
                "To": "+18888210810",
                "Body": "STOP",
                "MessageSid": f"SMstop{uuid.uuid4().hex[:6]}",
                "SmsStatus": "received",
            }
        ),
        defer_coach=True,
        app=None,
    )
    assert result["ok"] is True
    assert result["keyword"] == "opt_out"
    assert tdb.is_suppressed(u1, phone) is True
    row = next(r for r in tdb.list_suppressions(u1) if r["phone_number"] == phone)
    assert row["reason"] == "opted_out"
    assert row["source"] == "sms_keyword"
    assert "STOP_received" in _sms_audit_actions(u1)
    lead = db.get_lead(result["lead_id"], u1)
    assert lead["opt_out_status"] == "opted_out"


def test_twilio_non_stop_does_not_suppress(two_users, monkeypatch):
    from sms_inbound import parse_inbound_form, process_inbound_sms

    u1, _ = two_users
    phone = _unique_phone()
    _seed_lead(u1, phone)
    monkeypatch.setattr(config, "TWILIO_PHONE_NUMBER", "+18888210810")
    result = process_inbound_sms(
        parse_inbound_form(
            {
                "From": phone,
                "To": "+18888210810",
                "Body": "Yes, Saturday works",
                "MessageSid": f"SMok{uuid.uuid4().hex[:6]}",
                "SmsStatus": "received",
            }
        ),
        defer_coach=True,
        app=None,
    )
    assert result["ok"] is True
    assert tdb.is_suppressed(u1, phone) is False


# --- Governed removal: tenant_sms_db ----------------------------------------


def test_request_and_approve_removes(two_users):
    u1, u2 = two_users
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    req, err = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="Lead re-opted in by phone."
    )
    assert err is None and req is not None
    assert req["status"] == "pending"
    assert tdb.is_suppressed(u1, phone) is True  # request removes nothing
    reviewed, err = tdb.review_suppression_removal(
        req["id"], reviewer_user_id=u2, approve=True, note="Verified with lead."
    )
    assert err is None
    assert reviewed["status"] == "approved"
    assert reviewed["reviewed_by"] == u2
    assert tdb.is_suppressed(u1, phone) is False
    actions = _sms_audit_actions(u1)
    assert "suppression_removal_requested" in actions
    assert "suppression_removal_approved" in actions
    assert "suppression_removed" in actions


def test_self_review_blocked(two_users):
    u1, _ = two_users
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    req, _ = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="Please remove."
    )
    reviewed, err = tdb.review_suppression_removal(
        req["id"], reviewer_user_id=u1, approve=True
    )
    assert reviewed is None and err == "self_review"
    assert tdb.is_suppressed(u1, phone) is True
    assert tdb.list_suppression_removals(u1)[0]["status"] == "pending"


def test_request_requires_suppressed_number(two_users):
    u1, _ = two_users
    req, err = tdb.request_suppression_removal(
        u1, _unique_phone(), requested_by=u1, reason="Not suppressed."
    )
    assert req is None and err == "not_suppressed"


def test_request_requires_reason(two_users):
    u1, _ = two_users
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    req, err = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="   "
    )
    assert req is None and err == "reason_required"


def test_double_request_returns_existing_pending(two_users):
    u1, _ = two_users
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    first, err1 = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="First request."
    )
    assert err1 is None
    second, err2 = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="Second request."
    )
    assert err2 == "already_pending"
    assert second["id"] == first["id"]
    assert len(tdb.list_suppression_removals(u1)) == 1


def test_reject_keeps_suppressed(two_users):
    u1, u2 = two_users
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    req, _ = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="Please remove."
    )
    reviewed, err = tdb.review_suppression_removal(
        req["id"], reviewer_user_id=u2, approve=False, note="No evidence of re-consent."
    )
    assert err is None and reviewed["status"] == "rejected"
    assert tdb.is_suppressed(u1, phone) is True
    assert "suppression_removal_rejected" in _sms_audit_actions(u1)
    assert "suppression_removed" not in _sms_audit_actions(u1)


def test_review_twice_blocked(two_users):
    u1, u2 = two_users
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    req, _ = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="Please remove."
    )
    tdb.review_suppression_removal(req["id"], reviewer_user_id=u2, approve=True)
    reviewed, err = tdb.review_suppression_removal(
        req["id"], reviewer_user_id=u2, approve=True
    )
    assert reviewed is None and err == "already_reviewed"


def test_review_missing_request(two_users):
    _, u2 = two_users
    reviewed, err = tdb.review_suppression_removal(
        999999999, reviewer_user_id=u2, approve=True
    )
    assert reviewed is None and err == "not_found"


# --- Governed removal: HTTP routes ------------------------------------------


def test_routes_request_list_review_end_to_end(app_client, two_users, monkeypatch):
    u1, u2 = two_users
    monkeypatch.setenv("HARNESS_ADMIN_EMAILS", _email(u2))
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")

    _login(app_client, u1)
    res = app_client.get("/api/sms/suppression")
    assert res.status_code == 200
    assert any(s["phone_number"] == phone for s in res.get_json()["suppressions"])

    res = app_client.post(
        "/api/sms/suppression/removals",
        json={"phone_number": phone, "reason": "Lead called to re-opt in."},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    request_id = res.get_json()["removal"]["id"]
    assert tdb.is_suppressed(u1, phone) is True

    # The requester cannot review their own request — and is not an
    # admin at all, so the route refuses before the two-person rule.
    res = app_client.post(
        f"/api/sms/suppression/removals/{request_id}/review",
        json={"approve": True},
    )
    assert res.status_code == 403

    _login(app_client, u2)
    res = app_client.post(
        f"/api/sms/suppression/removals/{request_id}/review",
        json={"approve": True, "note": "Verified."},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["removal"]["status"] == "approved"
    assert tdb.is_suppressed(u1, phone) is False

    res = app_client.get("/api/sms/suppression/removals?scope=all")
    assert res.status_code == 200
    assert any(r["id"] == request_id for r in res.get_json()["removals"])


def test_routes_non_admin_review_forbidden(app_client, two_users, monkeypatch):
    u1, u2 = two_users
    monkeypatch.delenv("HARNESS_ADMIN_EMAILS", raising=False)
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    req, _ = tdb.request_suppression_removal(
        u1, phone, requested_by=u1, reason="Please remove."
    )
    _login(app_client, u2)
    res = app_client.post(
        f"/api/sms/suppression/removals/{req['id']}/review",
        json={"approve": True},
    )
    assert res.status_code == 403
    assert tdb.is_suppressed(u1, phone) is True


def test_routes_request_validates_phone_and_reason(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.post(
        "/api/sms/suppression/removals",
        json={"phone_number": "not-a-phone", "reason": "x"},
    )
    assert res.status_code == 400
    phone = _unique_phone()
    tdb.add_suppression(u1, phone, reason="opted_out", source="sms_keyword")
    res = app_client.post(
        "/api/sms/suppression/removals",
        json={"phone_number": phone, "reason": "   "},
    )
    assert res.status_code == 400
    res = app_client.post(
        "/api/sms/suppression/removals",
        json={"phone_number": _unique_phone(), "reason": "Not suppressed."},
    )
    assert res.status_code == 400
