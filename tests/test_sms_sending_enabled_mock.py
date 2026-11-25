"""is_sms_sending_enabled() mock-transport branch (SMS composer Send gate).

The AI SMS Assistant composer's "Send SMS" button is gated by
``is_sms_sending_enabled()`` (sms_authorization.py). For SMS_PROVIDER=telnyx
it required real Telnyx credentials AND toll-free verification, with no
mock branch — so on staging (no Telnyx API key) the button stayed disabled
even though the server-side send resolves to the mock provider
(HARNESS_MOCK_PROVIDERS, on by default in staging) and would send via mock.

The fix adds a mock-transport branch: return True when
``_resolved_transport_is_mock()`` is True, mirroring the waiver pattern in
``check_telnyx_toll_free_send_allowed`` and ``check_telnyx_trial_destination``.
On production the resolved transport is real Telnyx, so the new branch
cannot fire there.

Red-before-green: test_mock_transport_enabled fails on the unmodified code
(the telnyx branch returns False without creds); the other three pin
existing / fail-closed behavior and pass both before and after.
"""

from unittest.mock import patch

import config
from sms_authorization import is_sms_sending_enabled


def _telnyx_no_creds(monkeypatch):
    """Real Telnyx transport, no credentials, toll-free not verified."""
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_API_KEY", "")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")


def test_mock_transport_enabled(monkeypatch):
    """Mock resolved transport enables Send even with no Telnyx creds.

    Staging shape: SMS_PROVIDER=telnyx, no TELNYX_API_KEY, toll-free not
    verified — but HARNESS_MOCK_PROVIDERS makes the factory resolve to
    MockSMSProvider, so the send would go via mock. The gate must enable.
    FAILS on the unmodified code (telnyx branch -> False).
    """
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "TELNYX_API_KEY", "")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    assert is_sms_sending_enabled() is True


def test_real_telnyx_without_creds_disabled(monkeypatch):
    """Real Telnyx transport without an API key stays disabled."""
    _telnyx_no_creds(monkeypatch)
    assert is_sms_sending_enabled() is False


def test_real_telnyx_verified_enabled(monkeypatch):
    """Real Telnyx transport with creds + verified toll-free stays enabled."""
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    assert is_sms_sending_enabled() is True


def test_resolution_error_disabled(monkeypatch):
    """Fail-closed: a provider-resolution error leaves Send disabled.

    When get_sms_provider() raises, _resolved_transport_is_mock() returns
    False and the gate falls through to the provider check (no creds ->
    False). The mock branch must never enable Send on a resolution error.
    """
    _telnyx_no_creds(monkeypatch)
    with patch(
        "sms_providers.factory.get_sms_provider",
        side_effect=RuntimeError("boom"),
    ):
        assert is_sms_sending_enabled() is False
