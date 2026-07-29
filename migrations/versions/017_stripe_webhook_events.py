"""Idempotent Stripe webhook event tracking."""

VERSION = "017_stripe_webhook_events"


def upgrade_sqlite(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_type "
        "ON stripe_webhook_events(event_type)"
    )


def upgrade_postgres(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_type "
        "ON stripe_webhook_events(event_type)"
    )
