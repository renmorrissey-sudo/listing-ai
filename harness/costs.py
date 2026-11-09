"""Cost ledger for the AI Harness (Phase 1) — observability only.

Records Anthropic token usage (from API response ``usage`` fields, which
the code today discards), plus any provider/tool usage the worker records
(Telnyx segments, Vapi minutes, SendGrid messages). No limits are
enforced here; enforcement and spending alerts are Phase 6.

Pricing — ESTIMATES, not billed charges
--------------------------------------
There is no Anthropic price feed in the app, so the table below
(:data:`PRICE_CENTS_PER_MILLION_TOKENS`) carries clearly-commented
ESTIMATE defaults, keyed by the model names in config.py
(CLAUDE_MODEL default ``claude-opus-4-6``; ASK_TOPAI_MODEL /
SCRIPT_CLAUDE_MODEL default ``claude-sonnet-5``). The values are
modelled on published Anthropic Sonnet/Opus-class list prices and have
NOT been verified against a live pricing page — do not present them as
what Anthropic billed. Token counts (``units``) are always exact;
``cost_cents`` is an ESTIMATE until the prices are confirmed against
Anthropic's pricing page and this table is updated.

Overriding the rates (no code change needed):

* ``HARNESS_ANTHROPIC_PRICES_JSON`` — a JSON object mapping a model
  name to ``{"input": <cents/1M>, "output": <cents/1M>}``, e.g.
  ``{"claude-sonnet-5": {"input": 300, "output": 1500}}``. Unknown
  models in the JSON are added; known models are replaced wholesale
  for that model.
* Per-model env vars (highest precedence, override the JSON too):
  ``HARNESS_ANTHROPIC_PRICE_<MODEL>_INPUT_CENTS_PER_M`` and
  ``HARNESS_ANTHROPIC_PRICE_<MODEL>_OUTPUT_CENTS_PER_M``, where
  ``<MODEL>`` is the model name upper-cased with every non-alphanumeric
  run replaced by ``_`` — e.g. ``claude-sonnet-5`` becomes
  ``HARNESS_ANTHROPIC_PRICE_CLAUDE_SONNET_5_INPUT_CENTS_PER_M``.

Every place a dollar figure derived from this table is shown must
label it an estimate: see :func:`format_cost_estimate` and the
``cost_basis="estimate"`` field on :func:`monthly_totals`. Never assert
an estimated cost is a billed charge.

**Confirm with Ren before relying on dollar figures.** When
confirming, update this table (or the env vars above) in one place —
no call site hard-codes a price.

Every query is user_id-scoped. All SQL uses ``?`` placeholders;
db_backend.CompatConnection translates them to ``%s`` for Postgres.
Timestamps are UTC ISO strings. No new dependencies.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import db

logger = logging.getLogger(__name__)

# Cost basis marker: every dollar figure derived from the pricing table
# below is an ESTIMATE, never a billed charge. monthly_totals() exposes
# this so any caller that displays a total can label it correctly.
COST_BASIS = "estimate"

ENV_ANTHROPIC_PRICES_JSON = "HARNESS_ANTHROPIC_PRICES_JSON"
ENV_ANTHROPIC_PRICE_PREFIX = "HARNESS_ANTHROPIC_PRICE_"

# ---------------------------------------------------------------------------
# Monthly spend cap (D2 enforcement).
#
# NOTE — DEFAULT CONFIRMED BY REN 2026-11-08: $50/tenant/month (5000 cents),
# roughly the $49 subscription price. Override per deployment with the
# HARNESS_MONTHLY_SPEND_CAP_CENTS env var.
# ---------------------------------------------------------------------------
DEFAULT_MONTHLY_SPEND_CAP_CENTS = 5000  # $50 per tenant per month (confirmed)

ENV_MONTHLY_SPEND_CAP_CENTS = "HARNESS_MONTHLY_SPEND_CAP_CENTS"


def get_monthly_spend_cap_cents() -> int:
    """Per-tenant monthly AI spend cap in cents.

    Reads ``HARNESS_MONTHLY_SPEND_CAP_CENTS`` from the environment (there
    is no existing harness env-var pattern in config.py, so it is read
    here directly); falls back to
    :data:`DEFAULT_MONTHLY_SPEND_CAP_CENTS` when unset, blank, or
    unparsable. A non-positive value disables the cap (returns 0, which
    callers treat as "no cap").
    """
    raw = os.environ.get(ENV_MONTHLY_SPEND_CAP_CENTS)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MONTHLY_SPEND_CAP_CENTS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "invalid %s=%r; using default cap %s cents",
            ENV_MONTHLY_SPEND_CAP_CENTS, raw, DEFAULT_MONTHLY_SPEND_CAP_CENTS,
        )
        return DEFAULT_MONTHLY_SPEND_CAP_CENTS
    return value


def current_month_spend_cents(user_id) -> int:
    """Tenant's spend in cents for the current UTC month (``harness_costs``).

    Sums ``cost_cents`` for the current YYYY-MM period. Returns 0 when
    the tenant has no rows, ``user_id`` is None, or the table is missing
    (e.g. the harness migration has not landed yet) — the gate must fail
    open on a missing ledger read, not crash, and a missing table means
    no spend has been recorded through it.
    """
    if user_id is None:
        return 0
    try:
        return int(monthly_totals(user_id, _current_period())["total_cents"])
    except Exception as exc:  # table missing, DB down, etc.
        logger.warning("harness current-month spend fell back to 0: %s", exc)
        return 0

# ---------------------------------------------------------------------------
# Anthropic pricing table — ESTIMATES (see module docstring). Cents per
# 1,000,000 tokens. Effective rates are built by _load_pricing_table()
# from these defaults plus the HARNESS_ANTHROPIC_PRICES_JSON and
# HARNESS_ANTHROPIC_PRICE_<MODEL>_*_CENTS_PER_M env overrides.
# ---------------------------------------------------------------------------
DEFAULT_PRICE_CENTS_PER_MILLION_TOKENS = {
    # Sonnet-class default (ASK_TOPAI_MODEL / SCRIPT_CLAUDE_MODEL).
    "claude-sonnet-5": {"input": 300, "output": 1500},
    # Opus-class default (CLAUDE_MODEL, listings).
    "claude-opus-4-6": {"input": 1500, "output": 7500},
    # Fallback for any other model string: Sonnet tier. Callers should
    # pass model= explicitly so the fallback is rarely used.
    "default": {"input": 300, "output": 1500},
}


def _model_env_name(model: str) -> str:
    """Env-var fragment for *model*: upper-cased, non-alphanumerics to ``_``."""
    return re.sub(r"[^A-Z0-9]+", "_", str(model or "").upper()).strip("_")


def _parse_rate(raw, *, source: str):
    """Parse one env/JSON rate override; None when missing/invalid."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("ignoring invalid Anthropic price override %s=%r", source, raw)
        return None
    if value < 0:
        logger.warning("ignoring negative Anthropic price override %s=%r", source, raw)
        return None
    return value


def _load_pricing_table() -> dict:
    """Effective pricing table: defaults <- JSON env <- per-model env vars.

    Re-reads the environment on every call so tests (and a process that
    reloads config) can override rates without restarting the module.
    Returns a fresh dict; callers must not mutate the module table.
    """
    import copy
    import json

    table = copy.deepcopy(DEFAULT_PRICE_CENTS_PER_MILLION_TOKENS)

    raw_json = os.environ.get(ENV_ANTHROPIC_PRICES_JSON)
    if raw_json is not None and str(raw_json).strip() != "":
        try:
            overrides = json.loads(raw_json)
        except (TypeError, ValueError):
            logger.warning(
                "ignoring invalid %s (not JSON): %r", ENV_ANTHROPIC_PRICES_JSON, raw_json
            )
            overrides = None
        if isinstance(overrides, dict):
            for model, rates in overrides.items():
                if not isinstance(rates, dict):
                    logger.warning(
                        "ignoring invalid %s entry for model %r (not an object)",
                        ENV_ANTHROPIC_PRICES_JSON, model,
                    )
                    continue
                parsed = {}
                for field in ("input", "output"):
                    value = _parse_rate(
                        rates.get(field),
                        source=f"{ENV_ANTHROPIC_PRICES_JSON}[{model}][{field}]",
                    )
                    if value is not None:
                        parsed[field] = value
                if parsed:
                    merged = dict(table.get(model) or table["default"])
                    merged.update(parsed)
                    table[str(model)] = merged

    for model in list(table):
        fragment = _model_env_name(model)
        if not fragment:
            continue
        for field, suffix in (("input", "INPUT"), ("output", "OUTPUT")):
            env_name = (
                f"{ENV_ANTHROPIC_PRICE_PREFIX}{fragment}_{suffix}_CENTS_PER_M"
            )
            value = _parse_rate(os.environ.get(env_name), source=env_name)
            if value is not None:
                table[model][field] = value

    # Per-model env vars for models NOT in the table yet (e.g. a new
    # model id): the fragment lower-cased with "_" -> "-" recovers the
    # model name for the hyphenated ids this codebase uses. Collect
    # both fields first so INPUT and OUTPUT for the same new model
    # land in one entry.
    env_re = re.compile(
        r"^" + re.escape(ENV_ANTHROPIC_PRICE_PREFIX) + r"(.+)_(INPUT|OUTPUT)_CENTS_PER_M$"
    )
    new_models: dict[str, dict] = {}
    for env_name, raw in os.environ.items():
        match = env_re.match(env_name)
        if not match:
            continue
        model = match.group(1).lower().replace("_", "-")
        if model in table:
            continue  # already applied above
        value = _parse_rate(raw, source=env_name)
        if value is None:
            continue
        new_models.setdefault(model, {})[
            "input" if match.group(2) == "INPUT" else "output"
        ] = value
    for model, fields in new_models.items():
        entry = dict(table["default"])
        entry.update(fields)
        table[model] = entry

    return table


# Effective table at import time (back-compat name). Prefer
# get_pricing_table() / get_model_prices(), which re-read env overrides.
PRICE_CENTS_PER_MILLION_TOKENS = _load_pricing_table()


def get_pricing_table() -> dict:
    """Effective per-model pricing table (cents per 1M tokens), ESTIMATES.

    Returns a copy with env overrides applied; see the module docstring
    for ``HARNESS_ANTHROPIC_PRICES_JSON`` and the per-model env vars.
    """
    return _load_pricing_table()


def get_model_prices(model) -> dict:
    """``{"input": ..., "output": ...}`` cents/1M tokens for *model*.

    Unknown/empty model strings fall back to the ``"default"``
    (Sonnet-tier) rates. Rates are ESTIMATES — see module docstring.
    """
    table = _load_pricing_table()
    return dict(table.get(model or "default", table["default"]))


def format_cost_estimate(cost_cents) -> str:
    """Display string for a cost derived from the pricing table.

    Always labels the figure an estimate so no UI, log, or alert can
    present it as a billed charge, e.g. ``"$0.42 (estimated)"``.
    """
    try:
        dollars = int(cost_cents or 0) / 100
    except (TypeError, ValueError):
        dollars = 0
    return f"${dollars:,.2f} (estimated — not a billed charge)"


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
    """Estimated cost in cents for one Anthropic call (ESTIMATE, not billed).

    Token counts are exact; the rates come from :func:`get_model_prices`
    and are estimates until confirmed against Anthropic's pricing page.
    """
    prices = get_model_prices(model)
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
    the price tier from :func:`get_model_prices`; when omitted the
    'default' (Sonnet-tier) prices apply, so callers that know the model
    should pass it explicitly. The stored ``cost_cents`` is an ESTIMATE
    (see module docstring) — token counts (``units``) are exact. Returns
    the cost row id.
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
    """Return {by_provider, by_tool, total_cents, cost_basis} for one YYYY-MM period.

    ``by_provider`` / ``by_tool`` map name -> cost cents summed over the
    period. ``total_cents`` is the period total across all providers.
    ``cost_basis`` is always ``"estimate"``: the cents are derived from
    the Anthropic pricing table in this module, which is an estimate —
    never present ``total_cents`` as a billed charge; use
    :func:`format_cost_estimate` when displaying it.
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
        "cost_basis": COST_BASIS,
    }


def list_costs(user_id, period=None, limit=100):
    """List a tenant's cost rows, newest first, optionally for one period.

    Each row's ``cost_cents`` is an ESTIMATE (see module docstring);
    ``units`` (token counts) are exact.
    """
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
