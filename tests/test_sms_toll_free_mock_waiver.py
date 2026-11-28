"""Composer toll-free verification reporting under the mock transport.

GET /sms/messages (the AI SMS Assistant composer data load) reports
``toll_free_verification_blocked`` and ``toll_free_verification_status``,
which the composer JS (templates/index.html, loadSmsMessages) turns into
``smsTollFreeVerified`` — one of the three conjuncts enabling the
"Send SMS" button (syncSmsSendEnabled). PR #60 fixed the
``sms_sending_enabled`` gate, but on staging the button stayed disabled
because ``verification_blocked`` was True: the composer data load computed
it from ``is_telnyx_toll_free_verified()`` with no mock-transport waiver,
and the mock provider's ``get_sender_information()`` carries no toll-free
keys.

The fix: when ``_resolved_transport_is_mock()`` is True, the composer data
load reports ``verification_blocked`` False AND
``toll_free_verification_status`` 'verified' (plus
``status["toll_free_verified"] = True``), mirroring the waiver already in
``check_telnyx_toll_free_send_allowed``. On production the resolved
transport is real Telnyx, so the waiver cannot fire there.

Red-before-green: test_mock_transport_reports_verified fails on the
unmodified code (blocked True, status 'pending'); the other two pin
existing real-Telnyx behavior and pass both before and after.
"""

import config


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _composer_data(app_client, user_id):
    _login(app_client, user_id)
    res = app_client.get("/sms/messages")
    assert res.status_code == 200
    return res.get_json()


def test_mock_transport_reports_verified(app_client, two_users, monkeypatch):
    """Mock resolved transport: not blocked, status 'verified'.

    Staging shape: SMS_PROVIDER=telnyx, no TELNYX_API_KEY, toll-free not
    verified — but HARNESS_MOCK_PROVIDERS makes the factory resolve to
    MockSMSProvider, so no real Telnyx send happens and toll-free
    verification is vacuous. The composer JSON must report the waiver.
    FAILS on the unmodified code (blocked True, status 'pending').
    """
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "TELNYX_API_KEY", "")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    data = _composer_data(app_client, u1)
    assert data["toll_free_verification_blocked"] is False
    assert data["toll_free_verification_status"] == "verified"
    assert data["provider_status"]["toll_free_verified"] is True
    assert data["verification_block_message"] is None
    # The resolved provider is the mock, so the composer reports it as such.
    assert data["sms_provider"] == "mock"
    assert data["sms_sending_enabled"] is True


def test_real_telnyx_unverified_stays_blocked(app_client, two_users, monkeypatch):
    """Real Telnyx transport, unverified: blocked, status as-is."""
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    data = _composer_data(app_client, u1)
    assert data["toll_free_verification_blocked"] is True
    assert data["toll_free_verification_status"] == "pending"
    assert data["sms_provider"] == "telnyx"
    assert data["sms_sending_enabled"] is False


def test_real_telnyx_verified_not_blocked(app_client, two_users, monkeypatch):
    """Real Telnyx transport, verified: not blocked, status 'verified'."""
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    data = _composer_data(app_client, u1)
    assert data["toll_free_verification_blocked"] is False
    assert data["toll_free_verification_status"] == "verified"
    assert data["sms_provider"] == "telnyx"
    assert data["sms_sending_enabled"] is True
