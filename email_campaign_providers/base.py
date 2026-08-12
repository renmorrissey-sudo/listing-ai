"""Provider interface and safe error types for email campaign drafts."""

from __future__ import annotations


class EmailCampaignProviderError(RuntimeError):
    def __init__(
        self,
        user_message: str,
        *,
        error_code: str | None = None,
        uncertain: bool = False,
    ):
        super().__init__(user_message)
        self.user_message = user_message
        self.error_code = error_code
        self.uncertain = uncertain


class BaseEmailCampaignProvider:
    name = ""
    display_name = ""

    def test_connection(self):
        raise NotImplementedError

    def get_senders(self):
        raise NotImplementedError

    def get_lists(self):
        raise NotImplementedError

    def get_suppression_groups(self):
        raise NotImplementedError

    def create_draft(self, **kwargs):
        raise NotImplementedError
