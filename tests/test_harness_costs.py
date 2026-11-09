"""L3: Anthropic pricing table — per-model rates, env overrides, estimates.

Rates in harness/costs.py are ESTIMATES, never billed charges. These
tests pin: the default per-model table, JSON + per-model env overrides,
exact token counts with estimated cents, and the estimate labelling on
every surface that shows a dollar figure (format_cost_estimate,
monthly_totals cost_basis). No network, no real provider.
"""

import pytest

from harness import costs

ENV_VARS = (
    "HARNESS_ANTHROPIC_PRICES_JSON",
    "HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_INPUT_CENTS_PER_M",
    "HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_OUTPUT_CENTS_PER_M",
    "HARNESS_ANTHROPIC_PRICE_CLAUDE_OPUS_4_6_INPUT_CENTS_PER_M",
    "HARNESS_ANTHROPIC_PRICE_CLAUDE_OPUS_4_6_OUTPUT_CENTS_PER_M",
    "HARNESS_ANTHROPIC_PRICE_CLAUDE_HAIKU_9_INPUT_CENTS_PER_M",
    "HARNESS_ANTHROPIC_PRICE_CLAUDE_HAIKU_9_OUTPUT_CENTS_PER_M",
)


@pytest.fixture(autouse=True)
def _clean_price_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_default_pricing_table_is_per_model():
    table = costs.get_pricing_table()
    assert table["claude-sonnet-5"] == {"input": 300, "output": 1500}
    assert table["claude-opus-4-6"] == {"input": 1500, "output": 7500}
    assert table["default"] == {"input": 300, "output": 1500}
    # The import-time table carries the same defaults.
    assert costs.PRICE_CENTS_PER_MILLION_TOKENS["claude-sonnet-5"] == {
        "input": 300,
        "output": 1500,
    }


def test_unknown_model_falls_back_to_default_tier():
    assert costs.get_model_prices("claude-something-new") == {
        "input": 300,
        "output": 1500,
    }
    assert costs.get_model_prices(None) == {"input": 300, "output": 1500}
    assert costs.get_model_prices("") == {"input": 300, "output": 1500}


def test_per_model_env_override(monkeypatch):
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_INPUT_CENTS_PER_M", "600"
    )
    prices = costs.get_model_prices("claude-sonnet-5")
    assert prices == {"input": 600, "output": 1500}
    # Other models are untouched.
    assert costs.get_model_prices("claude-opus-4-6") == {
        "input": 1500,
        "output": 7500,
    }


def test_env_override_for_a_model_not_in_the_table(monkeypatch):
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICE_CLAUDE_HAIKU_9_INPUT_CENTS_PER_M", "80"
    )
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICE_CLAUDE_HAIKU_9_OUTPUT_CENTS_PER_M", "400"
    )
    assert costs.get_model_prices("claude-haiku-9") == {
        "input": 80,
        "output": 400,
    }


def test_json_env_override(monkeypatch):
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICES_JSON",
        '{"claude-opus-4-6": {"input": 100, "output": 200}}',
    )
    assert costs.get_model_prices("claude-opus-4-6") == {
        "input": 100,
        "output": 200,
    }
    assert costs.get_model_prices("claude-sonnet-5") == {
        "input": 300,
        "output": 1500,
    }


def test_per_model_env_beats_json_override(monkeypatch):
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICES_JSON",
        '{"claude-sonnet-5": {"input": 100, "output": 200}}',
    )
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_OUTPUT_CENTS_PER_M", "999"
    )
    assert costs.get_model_prices("claude-sonnet-5") == {
        "input": 100,
        "output": 999,
    }


def test_invalid_overrides_are_ignored_not_fatal(monkeypatch):
    monkeypatch.setenv("HARNESS_ANTHROPIC_PRICES_JSON", "{not json")
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_INPUT_CENTS_PER_M", "abc"
    )
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICE_CLAUDE_OPUS_4_6_OUTPUT_CENTS_PER_M", "-5"
    )
    assert costs.get_model_prices("claude-sonnet-5") == {
        "input": 300,
        "output": 1500,
    }
    assert costs.get_model_prices("claude-opus-4-6") == {
        "input": 1500,
        "output": 7500,
    }


def test_pricing_table_returns_a_copy():
    table = costs.get_pricing_table()
    table["claude-sonnet-5"]["input"] = 1
    assert costs.get_model_prices("claude-sonnet-5")["input"] == 300


def test_record_anthropic_usage_keeps_tokens_exact_and_cost_estimated(
    two_users,
):
    u1, _ = two_users
    row_id = costs.record_anthropic_usage(
        u1,
        "ask_topai",
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        model="claude-sonnet-5",
    )
    assert row_id
    rows = costs.list_costs(u1)
    row = next(r for r in rows if r["id"] == row_id)
    assert row["units"] == 2_000_000  # exact token count
    assert row["unit_type"] == "tokens"
    assert row["cost_cents"] == 1800  # 300 + 1500, estimate at default rates


def test_record_anthropic_usage_uses_model_tier(two_users):
    u1, _ = two_users
    row_id = costs.record_anthropic_usage(
        u1,
        "ask_topai",
        {"input_tokens": 1_000_000, "output_tokens": 0},
        model="claude-opus-4-6",
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["cost_cents"] == 1500
    # Unknown model -> default (Sonnet) tier.
    row_id = costs.record_anthropic_usage(
        u1,
        "ask_topai",
        {"input_tokens": 500_000, "output_tokens": 0},
        model="claude-future-1",
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["cost_cents"] == 150


def test_recorded_cost_follows_env_override(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setenv(
        "HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_INPUT_CENTS_PER_M", "600"
    )
    row_id = costs.record_anthropic_usage(
        u1,
        "ask_topai",
        {"input_tokens": 1_000_000, "output_tokens": 0},
        model="claude-sonnet-5",
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["cost_cents"] == 600


def test_cost_figures_are_labelled_estimates(two_users):
    u1, _ = two_users
    costs.record_anthropic_usage(
        u1,
        "ask_topai",
        {"input_tokens": 1000, "output_tokens": 1000},
        model="claude-sonnet-5",
    )
    totals = costs.monthly_totals(u1, costs._current_period())
    assert totals["cost_basis"] == "estimate"
    assert totals["total_cents"] == 2  # (1000*300 + 1000*1500) / 1e6, rounded
    label = costs.format_cost_estimate(totals["total_cents"])
    assert "estimated" in label
    assert "not a billed charge" in label
    assert label.startswith("$0.02")
    assert costs.format_cost_estimate(0) == "$0.00 (estimated — not a billed charge)"


def test_spend_cap_default_is_unchanged():
    # L3 must not touch the $50/tenant/month cap (confirmed by Ren).
    assert costs.DEFAULT_MONTHLY_SPEND_CAP_CENTS == 5000
    assert costs.get_monthly_spend_cap_cents() == 5000
