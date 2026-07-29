"""SMS campaign worker heartbeat table for reliable worker health checks."""

VERSION = "016_sms_worker_heartbeats"


def upgrade_sqlite(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_worker_heartbeats (
            worker_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_worker_heartbeats_seen ON sms_worker_heartbeats(last_seen_at)"
    )


def upgrade_postgres(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_worker_heartbeats (
            worker_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_worker_heartbeats_seen ON sms_worker_heartbeats(last_seen_at)"
    )
