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

Non-Anthropic providers (launch blocker 5)
-------------------------------------------
Anthropic is priced per token (table above). Everything else is priced
per unit with a clearly-labelled PLACEHOLDER rate, because the repo
carries no verified price for these providers and there is no price
feed in the app:

* Vapi voice calls — cents per minute (``HARNESS_VAPI_PRICE_CENTS_PER_MINUTE``,
  default 10). ASSUMPTION: ~$0.10/min covers Vapi's platform + telephony
  at list; the model running inside the call is billed by Vapi on top
  of that in reality, so this is a conservative floor, not a quote.
* Telnyx / Twilio / SimpleTexting SMS — cents per segment/message
  (``HARNESS_TELNYX_PRICE_CENTS_PER_SEGMENT`` default 1,
  ``HARNESS_TWILIO_PRICE_CENTS_PER_MESSAGE`` default 1,
  ``HARNESS_SIMPLETEXTING_PRICE_CENTS_PER_MESSAGE`` default 2).
  ASSUMPTIONS, modelled on published US SMS list prices (Telnyx/Twilio
  ~$0.004–$0.008/segment). When Telnyx itself reports a cost amount in
  the send response, that provider-reported amount is used instead of
  the placeholder (see :func:`record_sms_send`).
* OpenAI Realtime voice sessions — cents per minute
  (``HARNESS_OPENAI_REALTIME_PRICE_CENTS_PER_MINUTE``, default 6).
  ASSUMPTION: the session is recorded at mint time as a 1-minute
  minimum; OpenAI bills audio + text tokens per minute in reality, so
  a long session costs more than the recorded row. Duration is not
  reported back to the server, so this cannot be trued up today.
* SendGrid email — cents per message
  (``HARNESS_SENDGRID_PRICE_CENTS_PER_MESSAGE``, default 1).
  ASSUMPTION: SendGrid list email is a fraction of a cent; 1 cent is a
  conservative ceiling.

All of these are ESTIMATES (``cost_basis="estimate"``) exactly like the
Anthropic rates. Confirm each against the provider's pricing page and
set the env var above before relying on a dollar figure.

Spend cap (universal enforcement)
----------------------------------
:func:`check_spend_cap` / :func:`is_spend_cap_tripped` are the single
gate every billable path must consult BEFORE spending. The cap is
$50/tenant/month by default (``HARNESS_MONTHLY_SPEND_CAP_CENTS``,
confirmed by Ren 2026-11-08); there is no per-tenant override column —
changing it is a deployment env change, see docs/ai-cost-coverage.md.
A ledger read failure fails OPEN (spend allowed) rather than crashing
the request, matching the pre-existing behaviour in harness.policy;
a cap check that cannot read spend must never take the product down.
Callers must still fail SAFE on a tripped cap: no model call, no
provider send, draft-only / block / queue-nothing-that-can-send.
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
# Universal spend-cap gate (launch blocker 5). Every billable path
# consults this BEFORE spending; see the module docstring.
# ---------------------------------------------------------------------------
SPEND_CAP_REACHED_MESSAGE = (
    "Monthly AI spend limit reached ($50/tenant/month by default). "
    "This action was not run and nothing was sent. The limit resets "
    "next month; contact support to raise it."
)


def spend_cap_status(user_id) -> dict:
    """``{cap_cents, spent_cents, remaining_cents, tripped}`` for a tenant.

    ``tripped`` is True when the cap is enabled (cap > 0) and the
    tenant's current-month recorded spend is at or above it. A ledger
    read failure fails open: spent is reported as 0 and tripped False,
    so a DB outage never takes the product down (same fail-open rule
    as :func:`current_month_spend_cents` and harness.policy).
    """
    cap_cents = get_monthly_spend_cap_cents()
    spent_cents = current_month_spend_cents(user_id) if cap_cents > 0 else 0
    return {
        "cap_cents": cap_cents,
        "spent_cents": spent_cents,
        "remaining_cents": max(cap_cents - spent_cents, 0) if cap_cents > 0 else None,
        "tripped": bool(cap_cents > 0 and spent_cents >= cap_cents),
    }


def is_spend_cap_tripped(user_id) -> bool:
    """True when the tenant's monthly spend cap is reached (fail open)."""
    try:
        return bool(spend_cap_status(user_id)["tripped"])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("harness spend-cap check failed open: %s", exc)
        return False


def check_spend_cap(user_id):
    """Pre-spend gate: ``(allowed, reason)``.

    ``allowed`` is False with :data:`SPEND_CAP_REACHED_MESSAGE` as the
    reason when the cap is tripped; otherwise ``(True, None)``. Callers
    must check this BEFORE any model call or provider send and, when
    False, fail safe (draft-only / block / send nothing).
    """
    if is_spend_cap_tripped(user_id):
        return False, SPEND_CAP_REACHED_MESSAGE
    return True, None


# ---------------------------------------------------------------------------
# Non-Anthropic placeholder pricing — ESTIMATES (see module docstring).
# Cents per unit. Env overrides are read on every call so tests and a
# reloaded process can change rates without restarting the module.
# ---------------------------------------------------------------------------
DEFAULT_VAPI_PRICE_CENTS_PER_MINUTE = 10
DEFAULT_OPENAI_REALTIME_PRICE_CENTS_PER_MINUTE = 6
DEFAULT_SENDGRID_PRICE_CENTS_PER_MESSAGE = 1
# Cents per SMS segment/message, by provider (mock sends cost nothing).
DEFAULT_SMS_PRICE_CENTS_PER_UNIT = {
    "telnyx": 1,
    "twilio": 1,
    "simpletexting": 2,
    "mock": 0,
    "default": 1,
}
ENV_VAPI_PRICE_PER_MINUTE = "HARNESS_VAPI_PRICE_CENTS_PER_MINUTE"
ENV_OPENAI_REALTIME_PRICE_PER_MINUTE = "HARNESS_OPENAI_REALTIME_PRICE_CENTS_PER_MINUTE"
ENV_SENDGRID_PRICE_PER_MESSAGE = "HARNESS_SENDGRID_PRICE_CENTS_PER_MESSAGE"
ENV_SMS_PRICE_PREFIX = "HARNESS_SMS_PRICE_CENTS_PER_"


def _parse_unit_rate(raw, *, source: str):
    """Parse one non-Anthropic rate override; None when missing/invalid."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("ignoring invalid non-Anthropic price override %s=%r", source, raw)
        return None
    if value < 0:
        logger.warning("ignoring negative non-Anthropic price override %s=%r", source, raw)
        return None
    return value


def get_vapi_price_cents_per_minute() -> int:
    """Vapi placeholder rate, cents/minute (ESTIMATE — see docstring)."""
    value = _parse_unit_rate(
        os.environ.get(ENV_VAPI_PRICE_PER_MINUTE), source=ENV_VAPI_PRICE_PER_MINUTE
    )
    return DEFAULT_VAPI_PRICE_CENTS_PER_MINUTE if value is None else value


def get_openai_realtime_price_cents_per_minute() -> int:
    """OpenAI Realtime placeholder rate, cents/minute (ESTIMATE)."""
    value = _parse_unit_rate(
        os.environ.get(ENV_OPENAI_REALTIME_PRICE_PER_MINUTE),
        source=ENV_OPENAI_REALTIME_PRICE_PER_MINUTE,
    )
    return DEFAULT_OPENAI_REALTIME_PRICE_CENTS_PER_MINUTE if value is None else value


def get_sendgrid_price_cents_per_message() -> int:
    """SendGrid placeholder rate, cents/message (ESTIMATE)."""
    value = _parse_unit_rate(
        os.environ.get(ENV_SENDGRID_PRICE_PER_MESSAGE),
        source=ENV_SENDGRID_PRICE_PER_MESSAGE,
    )
    return DEFAULT_SENDGRID_PRICE_CENTS_PER_MESSAGE if value is None else value


def get_sms_price_cents_per_unit(provider) -> int:
    """SMS placeholder rate, cents per segment/message (ESTIMATE).

    Per-provider env override:
    ``HARNESS_SMS_PRICE_CENTS_PER_<PROVIDER>_PER_UNIT`` (e.g.
    ``HARNESS_SMS_PRICE_CENTS_PER_TELNYX_PER_UNIT``); falls back to
    the table above, then to the ``"default"`` entry.
    """
    name = str(provider or "default").strip().lower()
    env_name = (
        f"{ENV_SMS_PRICE_PREFIX}{_model_env_name(name)}_PER_UNIT"
    )
    value = _parse_unit_rate(os.environ.get(env_name), source=env_name)
    if value is not None:
        return value
    return DEFAULT_SMS_PRICE_CENTS_PER_UNIT.get(
        name, DEFAULT_SMS_PRICE_CENTS_PER_UNIT["default"]
    )

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


def _insert_cost_row(conn, *, user_id, period, provider, tool, units,
                     unit_type, cost_cents, created_at, model=None,
                     correlation_id=None):
    """Insert one harness_costs row, using the model/correlation_id
    columns when they exist (migration 033) and falling back to the
    migration-029 column set when they do not."""
    try:
        cur = conn.execute(
            """
            INSERT INTO harness_costs
                (user_id, period, provider, tool, units, unit_type,
                 cost_cents, created_at, model, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                period,
                provider,
                tool,
                float(units or 0),
                unit_type,
                int(cost_cents or 0),
                created_at,
                (str(model)[:120] if model else None),
                (str(correlation_id)[:120] if correlation_id else None),
            ),
        )
        return cur.lastrowid
    except Exception:
        # Pre-033 schema (or a DB whose migrations have not run yet):
        # retry without the two new columns instead of losing the row.
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
                created_at,
            ),
        )
        return cur.lastrowid


def record_cost(user_id, provider, tool, units, unit_type, cost_cents=0,
                period=None, model=None, correlation_id=None):
    """Append one cost row and return its id.

    ``period`` defaults to the current YYYY-MM (UTC). ``unit_type`` is
    'tokens' | 'segments' | 'minutes' | 'messages' | 'sessions' |
    'calls'. ``units`` is a float (token/segment/minute/message/session/
    call count); ``cost_cents`` is an integer ESTIMATE (see module
    docstring). ``model`` is the provider model id when one applies
    (e.g. ``claude-sonnet-5``); ``correlation_id`` traces the row back
    to the request/event that caused the spend (inbound SMS id, Ask
    TopAI request id, Vapi provider call id, ...). Both are stored when
    migration 033 has landed and silently dropped otherwise.
    """
    now = _now()
    period = period or _current_period()
    with db.get_db() as conn:
        return _insert_cost_row(
            conn,
            user_id=user_id,
            period=period,
            provider=provider,
            tool=tool,
            units=units,
            unit_type=unit_type,
            cost_cents=cost_cents,
            created_at=now,
            model=model,
            correlation_id=correlation_id,
        )


def record_cost_once(user_id, provider, tool, units, unit_type, cost_cents=0,
                     period=None, model=None, correlation_id=None):
    """Idempotent :func:`record_cost`: no-op when a row with the same
    ``(user_id, tool, correlation_id)`` already exists in the period.

    Used for true-ups that can arrive more than once (e.g. a Vapi
    end-of-call webhook delivered twice). Returns the new row id, or
    None when the row already existed. Best-effort: any DB error is
    logged and returns None rather than raising into the caller.
    """
    if correlation_id is None:
        return record_cost(
            user_id, provider, tool, units, unit_type,
            cost_cents=cost_cents, period=period, model=model,
            correlation_id=None,
        )
    period = period or _current_period()
    try:
        with db.get_db() as conn:
            try:
                existing = conn.execute(
                    """
                    SELECT id FROM harness_costs
                    WHERE user_id = ? AND period = ? AND tool = ?
                      AND correlation_id = ?
                    LIMIT 1
                    """,
                    (user_id, period, tool, str(correlation_id)[:120]),
                ).fetchone()
            except Exception:
                existing = None  # pre-033 schema: no correlation_id column
            if existing:
                return None
            return _insert_cost_row(
                conn,
                user_id=user_id,
                period=period,
                provider=provider,
                tool=tool,
                units=units,
                unit_type=unit_type,
                cost_cents=cost_cents,
                created_at=_now(),
                model=model,
                correlation_id=correlation_id,
            )
    except Exception as exc:
        logger.warning("harness_costs idempotent write failed: %s", exc)
        return None


def record_anthropic_usage(user_id, tool, usage, model=None, correlation_id=None):
    """Record one Anthropic call's token usage and estimated cost.

    ``usage`` is an Anthropic response ``usage`` object (attribute access)
    or a dict with ``input_tokens`` / ``output_tokens``. ``model`` selects
    the price tier from :func:`get_model_prices`; when omitted the
    'default' (Sonnet-tier) prices apply, so callers that know the model
    should pass it explicitly. The stored ``cost_cents`` is an ESTIMATE
    (see module docstring) — token counts (``units``) are exact. Returns
    the cost row id, or None when the usage carries no tokens at all
    (e.g. a mocked response with no ``usage`` field — nothing was
    measurably spent, so no row is written).
    """
    input_tokens = _usage_tokens(usage, "input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    if input_tokens + output_tokens <= 0:
        return None
    cost_cents = _anthropic_cost_cents(model, input_tokens, output_tokens)
    return record_cost(
        user_id,
        provider="anthropic",
        tool=tool,
        units=input_tokens + output_tokens,
        unit_type="tokens",
        cost_cents=cost_cents,
        model=model,
        correlation_id=correlation_id,
    )


def record_anthropic_message(user_id, tool, message, model=None, correlation_id=None):
    """Record one Anthropic response message's usage (best-effort).

    ``message`` is the object returned by ``client.messages.create``;
    its ``usage`` attribute is read when present. Never raises — a
    recording failure must not break the user-facing request that
    already succeeded. Returns the cost row id or None.
    """
    if user_id is None or message is None:
        return None
    try:
        return record_anthropic_usage(
            user_id, tool, getattr(message, "usage", None),
            model=model, correlation_id=correlation_id,
        )
    except Exception as exc:
        logger.warning("harness anthropic cost recording failed: %s", exc)
        return None


def record_provider_usage(user_id, provider, tool, units, unit_type,
                          cost_cents=None, *, model=None, correlation_id=None,
                          rate_cents_per_unit=None):
    """Record one non-Anthropic provider usage row (best-effort).

    When ``cost_cents`` is None it is derived as
    ``round(units * rate_cents_per_unit)``; callers that already know a
    provider-reported amount pass ``cost_cents`` explicitly. The cost
    is an ESTIMATE unless the caller passed a provider-reported amount
    (see :func:`record_sms_send`). Never raises; returns the row id
    or None. ``user_id`` None means "no tenant to attribute to" — the
    row is skipped (callers must pass the tenant).
    """
    if user_id is None:
        return None
    try:
        if cost_cents is None:
            rate = rate_cents_per_unit if rate_cents_per_unit is not None else 0
            cost_cents = int(round(float(units or 0) * rate))
        return record_cost(
            user_id,
            provider=provider,
            tool=tool,
            units=units,
            unit_type=unit_type,
            cost_cents=cost_cents,
            model=model,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        logger.warning("harness provider cost recording failed: %s", exc)
        return None


def _sms_segments(result, message_body=None):
    """Segment count for an SMS send result; 1 when unknown.

    Telnyx reports ``segments`` (or ``parts``); other providers report
    nothing, in which case one message is one billable unit.
    """
    if isinstance(result, dict):
        for key in ("segments", "parts"):
            value = result.get(key)
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                return count
    return 1


def _provider_reported_cost_cents(result):
    """Telnyx-reported cost in cents, or None.

    Telnyx's send response carries ``cost: {"amount": "0.008", ...}``
    where the amount is USD. When present and parseable it is the
    provider's own figure, so it replaces the placeholder rate. Any
    other shape (missing, non-numeric, negative) falls back to the
    placeholder.
    """
    if not isinstance(result, dict):
        return None
    cost = result.get("cost")
    amount = None
    if isinstance(cost, dict):
        amount = cost.get("amount")
    elif isinstance(cost, (int, float, str)):
        amount = cost
    if amount is None:
        return None
    try:
        dollars = float(amount)
    except (TypeError, ValueError):
        return None
    if dollars < 0:
        return None
    return int(round(dollars * 100))


def record_sms_send(user_id, *, provider, tool, result=None,
                    message_body=None, correlation_id=None):
    """Record one outbound SMS send (best-effort, never raises).

    ``provider`` is the SMS provider name (``telnyx`` / ``twilio`` /
    ``simpletexting`` / ``mock``). ``result`` is the provider's send
    result dict. Cost: the Telnyx-reported amount when the provider
    supplied one, otherwise ``segments *`` the per-provider placeholder
    rate from :func:`get_sms_price_cents_per_unit` (mock sends cost 0).
    Returns the cost row id or None.
    """
    if user_id is None:
        return None
    segments = _sms_segments(result, message_body)
    reported = _provider_reported_cost_cents(result)
    if reported is not None:
        cost_cents = reported
    else:
        cost_cents = segments * get_sms_price_cents_per_unit(provider)
    return record_provider_usage(
        user_id,
        provider=str(provider or "unknown").lower(),
        tool=tool,
        units=segments,
        unit_type="segments",
        cost_cents=cost_cents,
        correlation_id=correlation_id,
    )


def record_vapi_call(user_id, *, tool="vapi_outbound_call", minutes=1.0,
                     correlation_id=None):
    """Record one Vapi outbound call (best-effort, never raises).

    The call's real duration is not known when the call is created, so
    the start records a 1-minute minimum at the placeholder per-minute
    rate; :func:`record_vapi_call_duration` trues up longer calls from
    the end-of-call webhook. Returns the cost row id or None.
    """
    return record_provider_usage(
        user_id,
        provider="vapi",
        tool=tool,
        units=float(minutes or 0),
        unit_type="minutes",
        rate_cents_per_unit=get_vapi_price_cents_per_minute(),
        correlation_id=correlation_id,
    )


def record_vapi_call_duration(user_id, *, duration_seconds, correlation_id=None):
    """True up a Vapi call's cost from its end-of-call duration.

    Records ONLY the minutes beyond the 1-minute minimum already
    recorded at call start, as tool ``vapi_call_duration``, idempotently
    per ``correlation_id`` (the Vapi provider call id) — a redelivered
    webhook must not double-charge the ledger. Returns the row id, or
    None when there is nothing to true up (<= 60s, no duration, or the
    true-up row already exists).
    """
    if user_id is None or correlation_id is None:
        return None
    try:
        total_minutes = -(-int(duration_seconds) // 60)  # ceil
    except (TypeError, ValueError):
        return None
    extra_minutes = total_minutes - 1
    if extra_minutes <= 0:
        return None
    return record_cost_once(
        user_id,
        provider="vapi",
        tool="vapi_call_duration",
        units=float(extra_minutes),
        unit_type="minutes",
        cost_cents=extra_minutes * get_vapi_price_cents_per_minute(),
        correlation_id=correlation_id,
    )


def record_realtime_session(user_id, *, model=None, correlation_id=None):
    """Record one OpenAI Realtime session mint (best-effort, never raises).

    The browser session's real audio/token usage is never reported back
    to the server, so the row records a 1-minute minimum at the
    placeholder per-minute rate (see module docstring) — a long session
    costs more than this row. Returns the cost row id or None.
    """
    return record_provider_usage(
        user_id,
        provider="openai",
        tool="realtime_session",
        units=1,
        unit_type="sessions",
        rate_cents_per_unit=get_openai_realtime_price_cents_per_minute(),
        model=model,
        correlation_id=correlation_id,
    )


def record_sendgrid_message(user_id, *, tool="lead_email", correlation_id=None):
    """Record one SendGrid email send (best-effort, never raises).

    One message at the placeholder per-message rate (ESTIMATE).
    Returns the cost row id or None.
    """
    return record_provider_usage(
        user_id,
        provider="sendgrid",
        tool=tool,
        units=1,
        unit_type="messages",
        rate_cents_per_unit=get_sendgrid_price_cents_per_message(),
        correlation_id=correlation_id,
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
