"""Base adapter for future provider-specific mappings. Core never auto-verifies consent."""

from __future__ import annotations


class ExternalLeadAdapter:
    provider_key = "generic"

    def normalize(self, payload: dict) -> dict:
        return dict(payload or {})


ADAPTER_REGISTRY = {}


def register_adapter(adapter: ExternalLeadAdapter):
    ADAPTER_REGISTRY[adapter.provider_key] = adapter


def get_adapter(provider_key: str) -> ExternalLeadAdapter:
    return ADAPTER_REGISTRY.get(provider_key) or ExternalLeadAdapter()


# Stub keys reserved for future adapters — none auto-verify consent.
for _key in (
    "zillow",
    "realtor_com",
    "readyconnect",
    "homes_com",
    "cinc",
    "ylopo",
    "market_leader",
    "real_geeks",
    "smartzip",
    "redx",
):

    class _Stub(ExternalLeadAdapter):
        provider_key = _key

    register_adapter(_Stub())
