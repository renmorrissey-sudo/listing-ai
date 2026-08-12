"""Additive billing fields on users for Stripe subscription sync and payment recovery.

- stripe_price_id: Stripe Price ID for the current subscription
- subscription_current_period_end: Unix timestamp of next renewal / period end
- payment_action_required: 1 when invoice/payment failed and user must update PM
- last_payment_error: Stripe decline/error code (e.g. link_connection_closed)
"""

VERSION = "019_billing_subscription_fields"

USER_COLS_SQLITE = [
    ("stripe_price_id", "TEXT"),
    ("subscription_current_period_end", "INTEGER"),
    ("payment_action_required", "INTEGER NOT NULL DEFAULT 0"),
    ("last_payment_error", "TEXT"),
]

USER_COLS_PG = [
    ("stripe_price_id", "TEXT"),
    ("subscription_current_period_end", "BIGINT"),
    ("payment_action_required", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("last_payment_error", "TEXT"),
]


def _sqlite_has_column(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {dict(r)["name"] if hasattr(r, "keys") else r[1] for r in rows}
    return column in names


def _postgres_has_column(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return bool(row)


def _add_columns(conn, table, columns, *, is_postgres):
    for name, typ in columns:
        has = (
            _postgres_has_column(conn, table, name)
            if is_postgres
            else _sqlite_has_column(conn, table, name)
        )
        if has:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def upgrade_sqlite(conn):
    _add_columns(conn, "users", USER_COLS_SQLITE, is_postgres=False)


def upgrade_postgres(conn):
    _add_columns(conn, "users", USER_COLS_PG, is_postgres=True)
