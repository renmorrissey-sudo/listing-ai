"""Capability-aware provider interface and safe errors for email drafts."""

from __future__ import annotations

from dataclasses import asdict, dataclass


class EmailCampaignProviderError(RuntimeError):
    def __init__(
        self,
        user_message: str,
        *,
        error_code: str | None = None,
        uncertain: bool = False,
        reconnect_required: bool = False,
    ):
        super().__init__(user_message)
        self.user_message = user_message
        self.error_code = error_code
        self.uncertain = uncertain
        self.reconnect_required = reconnect_required


@dataclass(frozen=True)
class EmailProviderCapabilities:
    provider: str
    label: str
    integration_kind: str
    auth_type: str
    supports_recipientless_draft: bool = False
    requires_sender: bool = False
    requires_list: bool = False
    supports_lists: bool = False
    supports_unsubscribe_groups: bool = False
    supports_token_refresh: bool = False
    supports_direct_send: bool = False

    def as_dict(self):
        return asdict(self)


class BaseEmailCampaignProvider:
    name = ""
    display_name = ""
    capabilities: EmailProviderCapabilities

    def test_connection(self):
        identity = self.get_identity()
        return {"ok": True, "identity": identity}

    def get_identity(self):
        return {}

    def get_authorization_url(self, *, redirect_uri, state, code_challenge=None):
        raise EmailCampaignProviderError(
            "OAuth is not supported by this email provider.",
            error_code="oauth_not_supported",
        )

    def exchange_code(self, *, code, redirect_uri, code_verifier=None):
        raise EmailCampaignProviderError(
            "OAuth is not supported by this email provider.",
            error_code="oauth_not_supported",
        )

    def refresh_access_token(self, refresh_token):
        raise EmailCampaignProviderError(
            "Token refresh is not supported by this email provider.",
            error_code="refresh_not_supported",
        )

    def get_senders(self):
        return []

    def get_lists(self):
        return []

    def get_suppression_groups(self):
        return []

    def create_draft(self, **kwargs):
        raise NotImplementedError

    def send_email(self, **kwargs):
        raise EmailCampaignProviderError(
            "This email provider cannot send one-to-one lead emails from TopAI.",
            error_code="send_not_supported",
        )
