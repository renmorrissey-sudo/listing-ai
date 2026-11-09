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

Precise fractional-cent costs (defect 2, migration 035)
-------------------------------------------------------
Costs are stored in fixed-point micro-cents (1 cent = 1,000,000
micros) in ``harness_costs.cost_micros``. ``_anthropic_cost_micros``
is exact integer arithmetic — the rates are integer cents per 1M
tokens, so ``input * input_rate + output * output_rate`` IS the
micro-cent cost, with no rounding at all. ``cost_cents`` is still
written (rounded, for display and backwards compatibility) and
aggregation/cap enforcement use ``cost_micros`` when the column
exists, degrading to ``cost_cents`` when it does not (same pattern
as the 033 columns). Display rounds to cents only at presentation.

Atomic spend reservation (defect 2, requirement 5)
--------------------------------------------------
A plain recheck of committed ledger totals is not safe under
concurrency: two workers can both read a sub-cap total and then both
spend, exceeding the cap. :func:`reserve_spend` closes that race.
Before a billable action, the caller reserves an upper-bound estimate
in ``harness_spend_reservations``; the check (committed + reserved +
estimate <= cap) and the reservation insert run in one transaction
that locks the tenant's ``harness_settings`` row (``SELECT ... FOR
UPDATE`` on Postgres; SQLite serializes writers), so concurrent
reservations for the same tenant are serialized. After the actual
cost is recorded the caller releases the reservation
(:func:`release_reservation`). Reservations expire after
``RESERVATION_TTL_MINUTES`` so a crashed worker cannot leak budget
forever; :func:`reserve_spend` reclaims expired rows for the tenant
on every call.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

# Fixed-point micro-cents: 1 cent = 1,000,000 micros. All precise
# cost arithmetic is integer micros; cents are a display rounding.
MICROS_PER_CENT = 1_000_000

# A reservation older than this is stale (its worker crashed or never
# reconciled) and is reclaimed by the next reserve for that tenant.
RESERVATION_TTL_MINUTES = 10

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


def current_month_spend_micros(user_id) -> int:
    """Tenant's precise spend in micro-cents for the current UTC month.

    Sums ``cost_micros`` when the column exists (migration 035);
    otherwise falls back to ``SUM(cost_cents) * MICROS_PER_CENT``
    (the 029 column set). Returns 0 when the tenant has no rows,
    ``user_id`` is None, or the table is missing — the gate must fail
    open on a missing ledger read, not crash.
    """
    if user_id is None:
        return 0
    try:
        return int(monthly_totals(user_id, _current_period())["total_micros"])
    except Exception as exc:  # table missing, DB down, etc.
        logger.warning("harness current-month spend fell back to 0: %s", exc)
        return 0


def current_month_spend_cents(user_id) -> int:
    """Tenant's spend in cents for the current UTC month (``harness_costs``).

    Derived from the precise micro-cent total (rounded down to whole
    cents) when the 035 column exists; otherwise the rounded
    ``cost_cents`` sum. Returns 0 when the tenant has no rows,
    ``user_id`` is None, or the table is missing (e.g. the harness
    migration has not landed yet) — the gate must fail open on a
    missing ledger read, not crash, and a missing table means no spend
    has been recorded through it.
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
    """``{cap_cents, spent_cents, remaining_cents, tripped, ...}`` for a tenant.

    ``tripped`` is True when the cap is enabled (cap > 0) and the
    tenant's current-month effective spend — committed ledger spend
    PLUS outstanding spend reservations (in-flight requests that have
    reserved budget but not yet recorded their cost) — is at or above
    it. The reservation term is what makes the cap hold under
    concurrency: a second worker reserving while the first is
    in-flight sees the first worker's reservation and cannot pass.
    ``spent_micros`` / ``committed_micros`` / ``reserved_micros``
    expose the precise split; ``spent_cents`` is the committed spend
    rounded down to whole cents (display). A ledger read failure fails
    open: spent is reported as 0 and tripped False, so a DB outage
    never takes the product down (same fail-open rule as
    :func:`current_month_spend_cents` and harness.policy).
    """
    cap_cents = get_monthly_spend_cap_cents()
    cap_micros = cap_cents * MICROS_PER_CENT if cap_cents > 0 else 0
    committed_micros = (
        current_month_spend_micros(user_id) if cap_cents > 0 else 0
    )
    reserved_micros = (
        reserved_spend_micros(user_id) if cap_cents > 0 else 0
    )
    spent_micros = committed_micros + reserved_micros
    return {
        "cap_cents": cap_cents,
        "cap_micros": cap_micros,
        "spent_cents": (committed_micros + MICROS_PER_CENT // 2) // MICROS_PER_CENT,
        "spent_micros": spent_micros,
        "committed_micros": committed_micros,
        "reserved_micros": reserved_micros,
        "remaining_cents": (
            max(
                cap_cents
                - (committed_micros + MICROS_PER_CENT // 2) // MICROS_PER_CENT,
                0,
            )
            if cap_cents > 0
            else None
        ),
        "remaining_micros": (
            max(cap_micros - spent_micros, 0) if cap_cents > 0 else None
        ),
        "tripped": bool(cap_cents > 0 and spent_micros >= cap_micros),
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

    NOTE: this is a point-in-time check and does NOT reserve budget —
    two workers that both pass it and then both spend can exceed the
    cap. Billable paths that run concurrently must use
    :func:`reserve_spend` (which checks AND reserves atomically)
    instead of, or in addition to, this check.
    """
    if is_spend_cap_tripped(user_id):
        return False, SPEND_CAP_REACHED_MESSAGE
    return True, None


# ---------------------------------------------------------------------------
# Atomic spend reservation (defect 2, requirement 5).
#
# reserve_spend() is the concurrency-safe cap gate. A plain recheck of
# committed ledger totals is not safe: two workers can both read a
# sub-cap total and then both spend. The reservation closes that race
# by atomically checking (committed + reserved + estimate <= cap) and
# inserting the reservation in one locked transaction, so the second
# worker sees the first worker's reservation and cannot pass.
# ---------------------------------------------------------------------------
def _reservations_table_exists(conn) -> bool:
    """True when harness_spend_reservations exists (migration 035)."""
    try:
        engine = getattr(conn, "engine", None)
        if engine == "postgres":
            row = conn.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'harness_spend_reservations'
                """
            ).fetchone()
            return row is not None
        rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'harness_spend_reservations'
            """
        ).fetchall()
        return len(rows) > 0
    except Exception:
        return False


def _delete_expired_reservations(conn, user_id, period, *, max_age_minutes=None):
    """Delete stale reservations for a tenant+period (crashed workers).

    Runs inside the caller's transaction so the reclaim and the new
    reservation are atomic. Returns the number of rows deleted.
    """
    ttl = RESERVATION_TTL_MINUTES if max_age_minutes is None else max_age_minutes
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ttl)).isoformat()
    cur = conn.execute(
        """
        DELETE FROM harness_spend_reservations
        WHERE user_id = ? AND period = ? AND created_at < ?
        """,
        (user_id, period, cutoff),
    )
    return cur.rowcount if cur.rowcount is not None else 0


def _lock_tenant_budget(conn, user_id):
    """Serialize concurrent reservations for one tenant.

    On Postgres, locks the tenant's harness_settings row (``SELECT ...
    FOR UPDATE``) so two workers reserving for the same tenant cannot
    interleave the check and the insert. On SQLite the write
    transaction already serializes writers, so no explicit lock is
    needed. The settings row is ensured to exist first (insert-if-
    missing with the seeded defaults) so there is always a row to lock.
    """
    engine = getattr(conn, "engine", None)
    now = _now()
    if engine == "postgres":
        conn.execute(
            """
            INSERT INTO harness_settings
                (user_id, mode, paused, ai_enabled, created_at, updated_at)
            VALUES (%s, 'drafts_only', 0, 1, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id, now, now),
        )
        conn.execute(
            "SELECT user_id FROM harness_settings WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
    else:
        conn.execute(
            """
            INSERT OR IGNORE INTO harness_settings
                (user_id, mode, paused, ai_enabled, created_at, updated_at)
            VALUES (?, 'drafts_only', 0, 1, ?, ?)
            """,
            (user_id, now, now),
        )


def reserved_spend_micros(user_id, period=None) -> int:
    """Outstanding reserved (in-flight, not yet recorded) spend in micros.

    Sums ``harness_spend_reservations`` for the tenant+period. Returns
    0 when the table is missing (pre-035 schema), the tenant has no
    reservations, or ``user_id`` is None. Never raises — a reservation
    read failure fails open (the cap check proceeds on committed spend
    alone), matching the fail-open rule for ledger reads.
    """
    if user_id is None:
        return 0
    period = period or _current_period()
    try:
        with db.get_db() as conn:
            if not _reservations_table_exists(conn):
                return 0
            row = conn.execute(
                """
                SELECT COALESCE(SUM(reserved_micros), 0) AS total
                FROM harness_spend_reservations
                WHERE user_id = ? AND period = ?
                """,
                (user_id, period),
            ).fetchone()
            total = row["total"] if isinstance(row, dict) else row[0]
            return int(total or 0)
    except Exception as exc:
        logger.warning("harness reserved-spend read failed open: %s", exc)
        return 0


def cleanup_expired_reservations(user_id=None, max_age_minutes=None) -> int:
    """Delete stale reservations (older than the TTL) and return the count.

    A crashed worker can leave a reservation behind; without reclaim
    the tenant's budget would stay reduced until the period rolls over.
    ``reserve_spend`` reclaims expired rows for the reserving tenant on
    every call; this function is the explicit / periodic sweep (all
    tenants when ``user_id`` is None). Never raises — returns 0 on error.
    """
    ttl = RESERVATION_TTL_MINUTES if max_age_minutes is None else max_age_minutes
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ttl)).isoformat()
    try:
        with db.get_db() as conn:
            if not _reservations_table_exists(conn):
                return 0
            if user_id is None:
                cur = conn.execute(
                    """
                    DELETE FROM harness_spend_reservations
                    WHERE created_at < ?
                    """,
                    (cutoff,),
                )
            else:
                cur = conn.execute(
                    """
                    DELETE FROM harness_spend_reservations
                    WHERE user_id = ? AND created_at < ?
                    """,
                    (user_id, cutoff),
                )
            return int(cur.rowcount or 0)
    except Exception as exc:
        logger.warning("harness reservation cleanup failed: %s", exc)
        return 0


def reserve_spend(user_id, estimated_micros, correlation_id) -> bool:
    """Atomically reserve *estimated_micros* of budget for one operation.

    Returns True when the reservation was made (or already existed for
    this ``correlation_id`` — a retry re-reserving is a no-op), False
    when the cap would be exceeded (committed + reserved + estimate >
    cap). Callers must treat False as "cap tripped": do NOT perform
    the billable action. After the action, the caller records the
    actual cost and calls :func:`release_reservation` (in a ``finally``
    so a failed action also releases its reservation).

    ``estimated_micros`` must be an UPPER BOUND on the action's cost
    (the actual recorded cost must be <= the estimate) so the cap can
    never be exceeded between the reservation and the recording. When
    the reservations table is missing (pre-035 schema) this falls back
    to the plain committed-spend check — the same fail-open behaviour
    as the rest of the cap gate.
    """
    if user_id is None:
        return True
    cap_cents = get_monthly_spend_cap_cents()
    if cap_cents <= 0:
        return True  # cap disabled
    estimated_micros = int(estimated_micros or 0)
    if estimated_micros <= 0:
        return True  # nothing to reserve
    cap_micros = cap_cents * MICROS_PER_CENT
    period = _current_period()
    try:
        with db.get_db() as conn:
            if not _reservations_table_exists(conn):
                # Pre-035 schema: no reservation table; fall back to the
                # plain committed-spend check in this transaction.
                committed_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(cost_cents), 0) * 1000000 AS total
                    FROM harness_costs
                    WHERE user_id = ? AND period = ?
                    """,
                    (user_id, period),
                ).fetchone()
                committed = committed_row["total"] if isinstance(committed_row, dict) else committed_row[0]
                return int(committed or 0) + estimated_micros <= cap_micros
            # Serialize concurrent reservations for this tenant, then
            # reclaim this tenant's expired rows in the same transaction.
            _lock_tenant_budget(conn, user_id)
            _delete_expired_reservations(conn, user_id, period)
            # Idempotent re-reserve: the same correlation_id already
            # holds a reservation (a retry) — it covers this call.
            existing = conn.execute(
                """
                SELECT reserved_micros FROM harness_spend_reservations
                WHERE user_id = ? AND period = ? AND correlation_id = ?
                """,
                (user_id, period, str(correlation_id)[:120]),
            ).fetchone()
            if existing is not None:
                return True
            # Committed spend: precise micros when the column exists,
            # otherwise the rounded cents total (same fallback as
            # monthly_totals).
            if _harness_costs_has_cost_micros(conn):
                committed_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(cost_micros), 0) AS total
                    FROM harness_costs
                    WHERE user_id = ? AND period = ?
                    """,
                    (user_id, period),
                ).fetchone()
            else:
                committed_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(cost_cents), 0) * 1000000 AS total
                    FROM harness_costs
                    WHERE user_id = ? AND period = ?
                    """,
                    (user_id, period),
                ).fetchone()
            committed = committed_row["total"] if isinstance(committed_row, dict) else committed_row[0]
            reserved_row = conn.execute(
                """
                SELECT COALESCE(SUM(reserved_micros), 0) AS total
                FROM harness_spend_reservations
                WHERE user_id = ? AND period = ?
                """,
                (user_id, period),
            ).fetchone()
            reserved = reserved_row["total"] if isinstance(reserved_row, dict) else reserved_row[0]
            if int(committed or 0) + int(reserved or 0) + estimated_micros > cap_micros:
                return False
            conn.execute(
                """
                INSERT INTO harness_spend_reservations
                    (user_id, period, correlation_id, reserved_micros, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    period,
                    str(correlation_id)[:120],
                    estimated_micros,
                    _now(),
                ),
            )
            return True
    except Exception as exc:
        logger.warning("harness spend reservation failed open: %s", exc)
        return True


def release_reservation(user_id, correlation_id) -> bool:
    """Release a reservation made by :func:`reserve_spend`.

    Deletes the reservation row for (user_id, current period,
    correlation_id). Call this in a ``finally`` after the billable
    action (whether it succeeded, failed, or was blocked after the
    reservation). Returns True when a row was deleted. Never raises.
    """
    if user_id is None or correlation_id is None:
        return False
    try:
        with db.get_db() as conn:
            if not _reservations_table_exists(conn):
                return False
            cur = conn.execute(
                """
                DELETE FROM harness_spend_reservations
                WHERE user_id = ? AND period = ? AND correlation_id = ?
                """,
                (user_id, _current_period(), str(correlation_id)[:120]),
            )
            return bool(cur.rowcount)
    except Exception as exc:
        logger.warning("harness reservation release failed: %s", exc)
        return False


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


def _anthropic_cost_micros(model, input_tokens, output_tokens):
    """Exact estimated cost in micro-cents for one Anthropic call.

    The rates are integer cents per 1M tokens, so
    ``input * input_rate + output * output_rate`` IS the micro-cent
    cost — exact integer arithmetic, no rounding at all. (One cent =
    1,000,000 micros; cost in cents = tokens * rate / 1e6, so cost in
    micros = tokens * rate.) Rates are ESTIMATES until confirmed
    against Anthropic's pricing page (see module docstring).
    """
    prices = get_model_prices(model)
    return int(input_tokens or 0) * prices["input"] + int(
        output_tokens or 0
    ) * prices["output"]


def _anthropic_cost_cents(model, input_tokens, output_tokens):
    """Estimated cost in cents for one Anthropic call (ESTIMATE, not billed).

    Token counts are exact; the rates come from :func:`get_model_prices`
    and are estimates until confirmed against Anthropic's pricing page.
    Rounded to the nearest cent from the exact micro-cent cost — display
    only; the ledger stores the exact micros (see :func:`record_cost`).
    """
    micros = _anthropic_cost_micros(model, input_tokens, output_tokens)
    return (micros + MICROS_PER_CENT // 2) // MICROS_PER_CENT


def estimate_anthropic_call_micros(model, prompt_chars, max_output_tokens=1200):
    """Upper-bound micro-cent estimate for one Anthropic call.

    Used for spend reservations (:func:`reserve_spend`): the estimate
    must be an UPPER BOUND on the actual cost so the cap can never be
    exceeded between the reservation and the recording. Input tokens
    are estimated at ~3 characters per token (a ~33% margin over the
    ~4 chars/token typical for English) and output at the caller's
    ``max_output_tokens`` (the hard cap on the response). The actual
    cost is recorded after the call and the reservation is released,
    so the over-estimate is transient (seconds).
    """
    input_est = max(int(prompt_chars or 0) // 3, 1)
    return _anthropic_cost_micros(model, input_est, max_output_tokens)


# Schema cache for _harness_costs_columns (see below).
# Cleared only by tests that change the schema mid-process.
_COSTS_COLUMNS_CACHE: dict = {}


def _harness_costs_columns(conn) -> set:
    """Live ``harness_costs`` column names, cached per process.

    Detected from the live schema — ``PRAGMA table_info`` on SQLite,
    ``information_schema`` on Postgres. Never raises: any detection
    failure returns an empty set (the safe fallback — no optional
    columns are assumed present). The schema does not change at
    runtime, so the result is cached.
    """
    cached = _COSTS_COLUMNS_CACHE.get("columns")
    if cached is not None:
        return cached
    try:
        engine = getattr(conn, "engine", None)
        if engine == "postgres":
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'harness_costs'
                """
            ).fetchall()
            columns = {
                (r["column_name"] if isinstance(r, dict) else r[0]) for r in rows
            }
        else:
            rows = conn.execute("PRAGMA table_info(harness_costs)").fetchall()
            columns = {
                (r["name"] if isinstance(r, dict) else r[1]) for r in rows
            }
    except Exception:
        columns = set()
    _COSTS_COLUMNS_CACHE["columns"] = columns
    return columns


def _harness_costs_has_extended_columns(conn) -> bool:
    """True when ``harness_costs`` has the 033 columns (model, correlation_id).

    Detected from the live schema before inserting (see
    :func:`_harness_costs_columns`) — never by trying the 033 INSERT
    and falling back on exception. That is not Postgres-safe: on
    Postgres a failed statement aborts the whole transaction, so the
    fallback INSERT in the same transaction failed with "current
    transaction is aborted" and the cost row was lost (defect 3 — the
    live staging SMS-send cost warning). Detecting the schema first
    means no statement ever fails for a missing column.
    """
    columns = _harness_costs_columns(conn)
    return "model" in columns and "correlation_id" in columns


def _harness_costs_has_cost_micros(conn) -> bool:
    """True when ``harness_costs`` has the 035 ``cost_micros`` column.

    Detected from the live schema (see :func:`_harness_costs_columns`).
    When absent (pre-035 schema) cost rows are written with
    ``cost_cents`` only and aggregation falls back to the rounded
    cents total.
    """
    return "cost_micros" in _harness_costs_columns(conn)


def _insert_cost_row(conn, *, user_id, period, provider, tool, units,
                     unit_type, cost_cents, created_at, model=None,
                     correlation_id=None, cost_micros=None):
    """Insert one harness_costs row, using the model/correlation_id
    columns when they exist (migration 033) and the cost_micros column
    when it exists (migration 035), falling back to the migration-029
    column set when they do not.

    ``cost_micros`` is the precise fixed-point cost (1 cent =
    1,000,000 micros). When None it is derived from ``cost_cents``
    (``cost_cents * MICROS_PER_CENT``). ``cost_cents`` is always
    written (rounded, display/compat). The column set is detected
    from the live schema BEFORE inserting (see
    :func:`_harness_costs_columns`) — never by trying the 033/035
    INSERT and catching its failure, which aborts the transaction on
    Postgres and loses the row (defect 3)."""
    if cost_micros is None:
        cost_micros = int(cost_cents or 0) * MICROS_PER_CENT
    cost_micros = int(cost_micros)
    columns = ["user_id", "period", "provider", "tool", "units", "unit_type",
               "cost_cents", "created_at"]
    values = [
        user_id,
        period,
        provider,
        tool,
        float(units or 0),
        unit_type,
        int(cost_cents or 0),
        created_at,
    ]
    if _harness_costs_has_extended_columns(conn):
        columns += ["model", "correlation_id"]
        values += [
            (str(model)[:120] if model else None),
            (str(correlation_id)[:120] if correlation_id else None),
        ]
    if _harness_costs_has_cost_micros(conn):
        columns += ["cost_micros"]
        values.append(cost_micros)
    placeholders = ", ".join(["?"] * len(columns))
    cur = conn.execute(
        f"INSERT INTO harness_costs ({', '.join(columns)}) "
        f"VALUES ({placeholders})",
        values,
    )
    return cur.lastrowid


def record_cost(user_id, provider, tool, units, unit_type, cost_cents=0,
                period=None, model=None, correlation_id=None,
                cost_micros=None):
    """Append one cost row and return its id.

    ``period`` defaults to the current YYYY-MM (UTC). ``unit_type`` is
    'tokens' | 'segments' | 'minutes' | 'messages' | 'sessions' |
    'calls'. ``units`` is a float (token/segment/minute/message/session/
    call count); ``cost_cents`` is an integer ESTIMATE (see module
    docstring). ``cost_micros`` is the precise fixed-point cost (1
    cent = 1,000,000 micros); when None it is derived from
    ``cost_cents``. Callers that know the exact micro-cent cost (e.g.
    Anthropic token math, a provider-reported dollar amount) should
    pass it explicitly so no rounding is lost. ``model`` is the
    provider model id when one applies (e.g. ``claude-sonnet-5``);
    ``correlation_id`` traces the row back to the request/event that
    caused the spend (inbound SMS id, Ask TopAI request id, Vapi
    provider call id, ...). Both are stored when migration 033 has
    landed and silently dropped otherwise.
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
            cost_micros=cost_micros,
        )


def record_cost_once(user_id, provider, tool, units, unit_type, cost_cents=0,
                     period=None, model=None, correlation_id=None,
                     cost_micros=None):
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
            correlation_id=None, cost_micros=cost_micros,
        )
    period = period or _current_period()
    try:
        with db.get_db() as conn:
            # The dedup SELECT references correlation_id, which only
            # exists on the 033 schema. Check the schema first — on
            # Postgres a failed SELECT aborts the transaction and the
            # insert below would fail with "current transaction is
            # aborted" (defect 3). On the 029 schema there is no
            # correlation_id to dedup on, so the row is always written
            # (the known D2 degradation: duplicate Vapi webhooks
            # over-count spend, failing safe).
            existing = None
            if _harness_costs_has_extended_columns(conn):
                existing = conn.execute(
                    """
                    SELECT id FROM harness_costs
                    WHERE user_id = ? AND period = ? AND tool = ?
                      AND correlation_id = ?
                    LIMIT 1
                    """,
                    (user_id, period, tool, str(correlation_id)[:120]),
                ).fetchone()
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
                cost_micros=cost_micros,
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
    cost_micros = _anthropic_cost_micros(model, input_tokens, output_tokens)
    cost_cents = (cost_micros + MICROS_PER_CENT // 2) // MICROS_PER_CENT
    return record_cost(
        user_id,
        provider="anthropic",
        tool=tool,
        units=input_tokens + output_tokens,
        unit_type="tokens",
        cost_cents=cost_cents,
        cost_micros=cost_micros,
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
                          rate_cents_per_unit=None, cost_micros=None):
    """Record one non-Anthropic provider usage row (best-effort).

    When ``cost_cents`` is None it is derived as
    ``round(units * rate_cents_per_unit)``; callers that already know a
    provider-reported amount pass ``cost_cents`` explicitly. When
    ``cost_micros`` is None it is derived from ``cost_cents``; callers
    that know the exact micro-cent cost (e.g. a provider-reported
    dollar amount) should pass it explicitly. The cost is an ESTIMATE
    unless the caller passed a provider-reported amount (see
    :func:`record_sms_send`). Never raises; returns the row id
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
            cost_micros=cost_micros,
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
    micros = _provider_reported_cost_micros(result)
    if micros is None:
        return None
    return (micros + MICROS_PER_CENT // 2) // MICROS_PER_CENT


def _provider_reported_cost_micros(result):
    """Telnyx-reported cost in micro-cents, or None (exact, no rounding).

    The provider amount is USD; micro-cents = dollars * 100 cents *
    1,000,000 micros/cent = dollars * 100,000,000. A typical Telnyx
    segment ($0.008) is 800,000 micros = 0.8 cents — exact, where the
    integer-cent version would round it to 1 cent.
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
    return int(round(dollars * 100 * MICROS_PER_CENT))


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
    reported_micros = _provider_reported_cost_micros(result)
    if reported_micros is not None:
        cost_micros = reported_micros
        cost_cents = (cost_micros + MICROS_PER_CENT // 2) // MICROS_PER_CENT
    else:
        cost_cents = segments * get_sms_price_cents_per_unit(provider)
        cost_micros = cost_cents * MICROS_PER_CENT
    return record_provider_usage(
        user_id,
        provider=str(provider or "unknown").lower(),
        tool=tool,
        units=segments,
        unit_type="segments",
        cost_cents=cost_cents,
        cost_micros=cost_micros,
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
    """Return {by_provider, by_tool, total_cents, total_micros, cost_basis} for one YYYY-MM period.

    ``by_provider`` / ``by_tool`` map name -> cost cents summed over the
    period. ``total_cents`` is the period total across all providers
    (rounded from the precise micro-cent total). ``total_micros`` is
    the precise fixed-point total (1 cent = 1,000,000 micros); it is
    what cap enforcement uses. When the 035 ``cost_micros`` column is
    absent (pre-migration schema) the micros total is derived from the
    rounded cents total. ``cost_basis`` is always ``"estimate"``: the
    cents are derived from the Anthropic pricing table in this module,
    which is an estimate — never present ``total_cents`` as a billed
    charge; use :func:`format_cost_estimate` when displaying it.
    """
    with db.get_db() as conn:
        if _harness_costs_has_cost_micros(conn):
            provider_rows = conn.execute(
                """
                SELECT provider, COALESCE(SUM(cost_micros), 0) AS micros
                FROM harness_costs
                WHERE user_id = ? AND period = ?
                GROUP BY provider
                """,
                (user_id, period),
            ).fetchall()
            tool_rows = conn.execute(
                """
                SELECT tool, COALESCE(SUM(cost_micros), 0) AS micros
                FROM harness_costs
                WHERE user_id = ? AND period = ?
                GROUP BY tool
                """,
                (user_id, period),
            ).fetchall()
        else:
            provider_rows = conn.execute(
                """
                SELECT provider, COALESCE(SUM(cost_cents), 0) * 1000000 AS micros
                FROM harness_costs
                WHERE user_id = ? AND period = ?
                GROUP BY provider
                """,
                (user_id, period),
            ).fetchall()
            tool_rows = conn.execute(
                """
                SELECT tool, COALESCE(SUM(cost_cents), 0) * 1000000 AS micros
                FROM harness_costs
                WHERE user_id = ? AND period = ?
                GROUP BY tool
                """,
                (user_id, period),
            ).fetchall()
    by_provider_micros = {r["provider"]: int(r["micros"]) for r in provider_rows}
    by_tool_micros = {r["tool"]: int(r["micros"]) for r in tool_rows}
    total_micros = sum(by_provider_micros.values())

    def _round_cents(micros):
        # Display cents round to the nearest cent (matches the
        # pre-035 behaviour of summing rounded per-row cost_cents).
        return (int(micros) + MICROS_PER_CENT // 2) // MICROS_PER_CENT

    return {
        "period": period,
        "by_provider": {
            name: _round_cents(micros)
            for name, micros in by_provider_micros.items()
        },
        "by_tool": {
            name: _round_cents(micros)
            for name, micros in by_tool_micros.items()
        },
        "total_cents": _round_cents(total_micros),
        "total_micros": total_micros,
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
