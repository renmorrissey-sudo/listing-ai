"""Cost ledger for the AI Harness (Phase 1) — observability only.

Records Anthropic token usage (from API response ``usage`` fields, which
the code today discards), plus any provider/tool usage the worker records
(Telnyx segments, Vapi minutes, SendGrid messages). No limits are
enforced here; enforcement and spending alerts are Phase 6.

Pricing
-------
No Anthropic price constants exist anywhere in config.py (verified
2026-11-08), so PRICE_CENTS_PER_MILLION_TOKENS below carries
clearly-commented placeholder defaults, keyed by the model names in
config.py (CLAUDE_MODEL default ``claude-opus-4-6``; ASK_TOPAI_MODEL /
SCRIPT_CLAUDE_MODEL default ``claude-sonnet-5``). The values are modelled
on published Anthropic Sonnet/Opus-class list prices and have NOT been
verified against a live pricing page.

**Confirm with Ren before relying on dollar figures.** Token counts
(``units``) are always exact; ``cost_cents`` is an estimate until the
prices are confirmed and this table is updated. When confirming, update
this dict in one place — no call site hard-codes a price.

Every query is user_id-scoped. All SQL uses ``?`` placeholders;
db_backend.CompatConnection translates them to ``%s`` for Postgres.
Timestamps are UTC ISO strings. No new dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import db

# ---------------------------------------------------------------------------
# PLACEHOLDER PRICING — confirm with Ren before relying on dollar figures.
# Cents per 1,000,000 tokens. See module docstring.
# ---------------------------------------------------------------------------
PRICE_CENTS_PER_MILLION_TOKENS = {
    # Sonnet-class default (ASK_TOPAI_MODEL / SCRIPT_CLAUDE_MODEL).
    "claude-sonnet-5": {"input": 300, "output": 1500},
    # Opus-class default (CLAUDE_MODEL, listings).
    "claude-opus-4-6": {"input": 1500, "output": 7500},
    # Fallback for any other model string: Sonnet tier. Callers should
    # pass model= explicitly so the fallback is rarely used.
    "default": {"input": 300, "output": 1500},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _current_period():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _row(row):
    return dict(row) if row is not None else None


def _usage_tokens(usage, field):
    """Read a token count from an Anthropic usage object or dict."""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(field)
    else:
        value = getattr(usage, field, None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _anthropic_cost_cents(model, input_tokens, output_tokens):
    prices = PRICE_CENTS_PER_MILLION_TOKENS.get(
        model or "default",
        PRICE_CENTS_PER_MILLION_TOKENS["default"],
    )
    cost = (
        input_tokens * prices["input"] + output_tokens * prices["output"]
    ) / 1_000_000
    return int(round(cost))


def record_cost(user_id, provider, tool, units, unit_type, cost_cents=0,
                period=None):
    """Append one cost row and return its id.

    ``period`` defaults to the current YYYY-MM (UTC). ``unit_type`` is
    'tokens' | 'segments' | 'minutes' | 'messages'. ``units`` is a float
    (token/segment/minute/message count); ``cost_cents`` is an integer.
    """
    now = _now()
    period = period or _current_period()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_costs
                (user_id, period, provider, tool, units, unit_type,
                 cost_cents, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                period,
                provider,
                tool,
                float(units or 0),
                unit_type,
                int(cost_cents or 0),
                now,
            ),
        )
        return cur.lastrowid


def record_anthropic_usage(user_id, tool, usage, model=None):
    """Record one Anthropic call's token usage and estimated cost.

    ``usage`` is an Anthropic response ``usage`` object (attribute access)
    or a dict with ``input_tokens`` / ``output_tokens``. ``model`` selects
    the price tier from PRICE_CENTS_PER_MILLION_TOKENS; when omitted the
    'default' (Sonnet-tier) prices apply, so callers that know the model
    should pass it explicitly. Returns the cost row id.
    """
    input_tokens = _usage_tokens(usage, "input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    cost_cents = _anthropic_cost_cents(model, input_tokens, output_tokens)
    return record_cost(
        user_id,
        provider="anthropic",
        tool=tool,
        units=input_tokens + output_tokens,
        unit_type="tokens",
        cost_cents=cost_cents,
    )


def monthly_totals(user_id, period):
    """Return {by_provider, by_tool, total_cents} for one YYYY-MM period.

    ``by_provider`` / ``by_tool`` map name -> cost cents summed over the
    period. ``total_cents`` is the period total across all providers.
    """
    with db.get_db() as conn:
        provider_rows = conn.execute(
            """
            SELECT provider, COALESCE(SUM(cost_cents), 0) AS cents
            FROM harness_costs
            WHERE user_id = ? AND period = ?
            GROUP BY provider
            """,
            (user_id, period),
        ).fetchall()
        tool_rows = conn.execute(
            """
            SELECT tool, COALESCE(SUM(cost_cents), 0) AS cents
            FROM harness_costs
            WHERE user_id = ? AND period = ?
            GROUP BY tool
            """,
            (user_id, period),
        ).fetchall()
    by_provider = {r["provider"]: int(r["cents"]) for r in provider_rows}
    by_tool = {r["tool"]: int(r["cents"]) for r in tool_rows}
    return {
        "period": period,
        "by_provider": by_provider,
        "by_tool": by_tool,
        "total_cents": sum(by_provider.values()),
    }


def list_costs(user_id, period=None, limit=100):
    """List a tenant's cost rows, newest first, optionally for one period."""
    with db.get_db() as conn:
        if period is None:
            rows = conn.execute(
                """
                SELECT * FROM harness_costs
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM harness_costs
                WHERE user_id = ? AND period = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, period, limit),
            ).fetchall()
    return [_row(r) for r in rows]
