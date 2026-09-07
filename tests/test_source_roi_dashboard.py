import uuid

import crm_db
import db
import external_leads_db
import source_roi_db
from migrations.runner import apply_pending_migrations


def _login(client, user_id):
    with client.session_transaction() as session:
        session["user_id"] = user_id


def _source(user_id, name, provider_key):
    return external_leads_db.create_external_lead_source(
        user_id, name=name, provider_key=provider_key
    )


def _lead(user_id, source_id, name, status="new"):
    lead_id = db.upsert_lead(
        user_id,
        f"+1555{uuid.uuid4().hex[:7]}",
        {"name": name, "lead_type": "buyer"},
        source="external",
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET external_source_id = ?, status = ? WHERE id = ? AND user_id = ?",
            (source_id, status, lead_id, user_id),
        )
    return lead_id


def test_source_roi_calculates_funnel_and_recommendation(two_users):
    apply_pending_migrations()
    user_id, _ = two_users
    zillow = _source(user_id, "Zillow", "zillow")
    redx = _source(user_id, "REDX", "redx")
    z1 = _lead(user_id, zillow, "Zillow Closing", "closed_won")
    z2 = _lead(user_id, zillow, "Zillow Conversation", "engaged")
    r1 = _lead(user_id, redx, "REDX Closing", "closed_won")

    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO sms_messages
              (user_id, lead_id, phone_number, message_body, direction, status, created_at)
            VALUES (?, ?, '+15550000000', 'Interested', 'inbound', 'received', '2026-09-01')
            """,
            (user_id, z2),
        )
    crm_db.create_appointment(
        user_id,
        {"lead_id": z1, "start_at": "2026-09-02T15:00:00+00:00", "appointment_type": "phone_call"},
    )
    assert source_roi_db.set_lead_gci(user_id, z1, 5000000)[1] is None
    assert source_roi_db.set_lead_gci(user_id, r1, 2500000)[1] is None
    assert source_roi_db.add_source_spend(user_id, zillow, 1000000, "2026-09-01")[1] is None
    assert source_roi_db.add_source_spend(user_id, redx, 100000, "2026-09-01")[1] is None

    result = source_roi_db.get_source_roi_dashboard(user_id)
    sources = {item["name"]: item for item in result["sources"]}
    assert sources["Zillow"]["leads"] == 2
    assert sources["Zillow"]["conversations"] == 1
    assert sources["Zillow"]["appointments"] == 1
    assert sources["Zillow"]["clients"] == 1
    assert sources["Zillow"]["closings"] == 1
    assert sources["Zillow"]["roi_multiple"] == 5.0
    assert sources["REDX"]["lead_label"] == "Prospects"
    assert result["recommendation"]["name"] == "REDX"
    assert result["recommendation"]["projected_gci_cents"] == 2500000


def test_roi_inputs_and_metrics_are_tenant_scoped(two_users):
    apply_pending_migrations()
    user_id, other_user_id = two_users
    source_id = _source(user_id, "Private Source", "private")
    lead_id = _lead(user_id, source_id, "Private Closing", "closed_won")

    assert source_roi_db.add_source_spend(other_user_id, source_id, 10000, "2026-09-01")[1]
    assert source_roi_db.set_lead_gci(other_user_id, lead_id, 50000)[1]
    assert source_roi_db.get_source_roi_dashboard(other_user_id)["sources"] == []


def test_source_roi_page_and_forms(app_client, two_users):
    apply_pending_migrations()
    user_id, _ = two_users
    source_id = _source(user_id, "Real Geeks", "real-geeks")
    lead_id = _lead(user_id, source_id, "Closed Client", "closed_won")
    _login(app_client, user_id)

    response = app_client.get("/crm/source-roi")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Lead Source ROI" in html
    assert "Where should I spend my next $1,000?" in html
    assert "Real Geeks" in html

    response = app_client.post(
        "/crm/source-roi/spend",
        data={"source_id": source_id, "amount": "11,600.00", "spent_on": "2026-09-01"},
    )
    assert response.status_code == 303
    response = app_client.post(
        "/crm/source-roi/gci", data={"lead_id": lead_id, "amount": "88,300.00"}
    )
    assert response.status_code == 303

    page = app_client.get("/crm/source-roi").get_data(as_text=True)
    assert "$88,300" in page
    assert "$11,600" in page
    assert "7.6&times; ROI" in page

    with db.get_db() as conn:
        spend_id = conn.execute(
            "SELECT id FROM external_lead_source_spend WHERE user_id = ? AND external_source_id = ?",
            (user_id, source_id),
        ).fetchone()["id"]
    response = app_client.post(f"/crm/source-roi/spend/{spend_id}/delete")
    assert response.status_code == 303
    assert source_roi_db.get_source_roi_dashboard(user_id)["total_cost_cents"] == 0
