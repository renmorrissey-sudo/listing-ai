"""Lead detail always presents a prioritized next-actions panel."""

import db
from crm import _build_lead_next_actions


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def test_new_lead_detail_puts_next_actions_before_consent(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    created = app_client.post(
        "/api/crm/leads",
        json={
            "first_name": "Action",
            "last_name": "Required",
            "phone": "+15551239876",
            "status": "new",
        },
    ).get_json()

    html = app_client.get(f"/crm/leads/{created['lead_id']}").get_data(as_text=True)

    assert 'id="lead-next-actions"' in html
    assert "Next Actions" in html
    assert "Make first contact" in html
    assert "Schedule the next touchpoint" in html
    assert "Verify consent before sending SMS" in html
    assert html.index('id="lead-next-actions"') < html.index('id="sms-consent-panel"')


def test_saved_next_action_is_prominent_on_lead_detail(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    lead_id = db.upsert_lead(
        user_id,
        "+15551239877",
        {
            "name": "Priority Plan",
            "lead_type": "buyer",
            "next_action": "Send three matching properties today",
        },
        source="sms",
    )
    db.update_lead_contact_info(
        lead_id,
        user_id,
        next_action="Send three matching properties today",
    )

    html = app_client.get(f"/crm/leads/{lead_id}").get_data(as_text=True)

    panel = html[html.index('id="lead-next-actions"'):html.index('id="sms-consent-panel"')]
    assert "Send three matching properties today" in panel
    assert "Saved next action for this lead." in panel


def test_do_not_contact_plan_suppresses_outreach_recommendations():
    actions = _build_lead_next_actions(
        {
            "status": "do_not_contact",
            "opt_out_status": "opted_out",
            "sms_consent_status": "opted_out",
            "sms_sending_blocked": 1,
            "next_action": "Call tomorrow",
        }
    )

    assert [action["title"] for action in actions] == ["Do not contact this lead"]
    assert actions[0]["tone"] == "danger"
