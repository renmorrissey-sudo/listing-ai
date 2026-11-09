"""L6: explicit Stripe timeouts + no silent retries of non-idempotent calls.

stripe-python v11 does not accept a per-call ``timeout`` kwarg (it would
be sent as an API parameter); the supported control is the HTTP client
(``stripe.default_http_client``). These tests pin, with a mocked Stripe
client, that the configured client carries the (connect, read) timeout,
that library-level automatic retries are off (the library default is 2),
and that paying an open invoice — a non-idempotent charge — is attempted
exactly once even when the network fails. No real Stripe calls.
"""

from unittest.mock import MagicMock, patch

import pytest
import stripe

import stripe_billing

ENV_VARS = (
    "STRIPE_CONNECT_TIMEOUT_SECONDS",
    "STRIPE_READ_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_stripe_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def _restore_stripe_client():
    saved_client = stripe.default_http_client
    saved_retries = stripe.max_network_retries
    try:
        yield
    finally:
        stripe.default_http_client = saved_client
        stripe.max_network_retries = saved_retries


def test_default_timeout_values():
    assert stripe_billing.STRIPE_CONNECT_TIMEOUT_SECONDS == 5
    assert stripe_billing.STRIPE_READ_TIMEOUT_SECONDS == 30
    assert stripe_billing.get_stripe_timeout() == (5, 30)


def test_env_override_for_timeouts(monkeypatch):
    monkeypatch.setenv("STRIPE_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("STRIPE_READ_TIMEOUT_SECONDS", "45")
    assert stripe_billing.get_stripe_timeout() == (2, 45)


def test_invalid_timeout_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("STRIPE_CONNECT_TIMEOUT_SECONDS", "abc")
    monkeypatch.setenv("STRIPE_READ_TIMEOUT_SECONDS", "-1")
    assert stripe_billing.get_stripe_timeout() == (5, 30)


def test_configure_installs_timeout_on_mocked_stripe_client(_restore_stripe_client):
    stripe.default_http_client = None
    with patch("stripe.RequestsClient") as mock_client_cls:
        stripe_billing.configure_stripe_http_client()
        mock_client_cls.assert_called_once_with(timeout=(5, 30))
    # Library-level automatic retries are disabled: the library default
    # is 2, and a silent re-attempt must never re-run a charge.
    assert stripe.max_network_retries == 0


def test_configure_is_idempotent_and_keeps_env_timeout(
    _restore_stripe_client, monkeypatch
):
    monkeypatch.setenv("STRIPE_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("STRIPE_READ_TIMEOUT_SECONDS", "45")
    stripe.default_http_client = None
    with patch("stripe.RequestsClient") as mock_client_cls:
        # The mock's built client reports the timeout it was built with,
        # so the second configure sees the desired timeout and no-ops.
        mock_client_cls.return_value._timeout = (2, 45)
        stripe_billing.configure_stripe_http_client()
        stripe_billing.configure_stripe_http_client()
        mock_client_cls.assert_called_once_with(timeout=(2, 45))


def test_real_configured_client_carries_the_timeout(_restore_stripe_client):
    stripe.default_http_client = None
    stripe_billing.configure_stripe_http_client()
    client = stripe.default_http_client
    assert client is not None
    assert getattr(client, "_timeout", None) == (5, 30)
    assert stripe.max_network_retries == 0


def test_non_idempotent_invoice_pay_is_not_retried(two_users):
    """A transient failure paying an open invoice: exactly one attempt.

    ``retry_open_subscription_invoice`` is a single deliberate call,
    not a retry loop — a non-idempotent charge must never be silently
    re-attempted.
    """
    u1, _ = two_users
    user = {"id": u1, "stripe_customer_id": "cus_test"}
    fake_sub = {"id": "sub_test"}
    fake_invoice = {"id": "in_test"}
    with (
        patch.object(
            stripe_billing, "primary_subscription_for_user", return_value=fake_sub
        ),
        patch.object(
            stripe_billing,
            "latest_open_invoice_for_subscription",
            return_value=fake_invoice,
        ),
        patch.object(
            stripe.Invoice,
            "pay",
            side_effect=stripe.APIConnectionError("boom"),
        ) as mock_pay,
    ):
        result = stripe_billing.retry_open_subscription_invoice(
            user, payment_method_id="pm_test"
        )
    assert mock_pay.call_count == 1
    assert result["status"] == "payment_failed"
    assert result["paid"] is False
    assert result["invoice_id"] == "in_test"


def test_checkout_sessions_use_a_stable_idempotency_key():
    """Idempotency-keyed writes (the safe kind to re-issue) are stable."""
    key1 = stripe_billing.checkout_idempotency_key(123)
    key2 = stripe_billing.checkout_idempotency_key(123)
    assert key1 == key2
    assert key1.startswith("subchk_123_")
    assert stripe_billing.checkout_idempotency_key(456) != key1
