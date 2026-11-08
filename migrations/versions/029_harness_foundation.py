"""AI Harness foundation tables (Phase 1).

Additive only. Creates harness_settings, harness_jobs, harness_audit,
harness_costs, harness_memory and harness_approvals, and seeds every
existing user with harness_settings mode='drafts_only', paused=0.

No DROP, no destructive changes. Follows migrations/versions/023_ask_topai.py
(SQLITE_TABLES / PG_TABLES / INDEXES lists) and 028_user_profile_phone.py
(verify(conn, dialect) plus verify_postgres / verify_sqlite).
"""

VERSION = "029_harness_foundation"

HARNESS_TABLES = (
    "harness_settings",
    "harness_jobs",
    "harness_audit",
    "harness_costs",
    "harness_memory",
    "harness_approvals",
)

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS harness_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        mode TEXT DEFAULT 'drafts_only',
        paused INTEGER DEFAULT 0,
        ai_enabled INTEGER DEFAULT 1,
        working_hours_json TEXT,
        messaging_limits_json TEXT,
        style_json TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT,
        idempotency_key TEXT UNIQUE,
        status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,
        next_attempt_at TEXT,
        claimed_at TEXT,
        claimed_by TEXT,
        result_json TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        actor TEXT,
        action TEXT,
        tool TEXT,
        target_type TEXT,
        target_id INTEGER,
        mode TEXT,
        gate_decision TEXT,
        gate_reason TEXT,
        cost_cents INTEGER DEFAULT 0,
        request_id TEXT,
        correlation_id TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        period TEXT,
        provider TEXT,
        tool TEXT,
        units REAL,
        unit_type TEXT,
        cost_cents INTEGER DEFAULT 0,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT,
        source TEXT,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE (user_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        job_id INTEGER,
        kind TEXT,
        draft_json TEXT,
        status TEXT DEFAULT 'pending',
        decided_by INTEGER,
        decided_at TEXT,
        created_at TEXT
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS harness_settings (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE,
        mode TEXT DEFAULT 'drafts_only',
        paused INTEGER DEFAULT 0,
        ai_enabled INTEGER DEFAULT 1,
        working_hours_json TEXT,
        messaging_limits_json TEXT,
        style_json TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_jobs (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT,
        idempotency_key TEXT UNIQUE,
        status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,
        next_attempt_at TEXT,
        claimed_at TEXT,
        claimed_by TEXT,
        result_json TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_audit (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT,
        actor TEXT,
        action TEXT,
        tool TEXT,
        target_type TEXT,
        target_id BIGINT,
        mode TEXT,
        gate_decision TEXT,
        gate_reason TEXT,
        cost_cents INTEGER DEFAULT 0,
        request_id TEXT,
        correlation_id TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_costs (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        period TEXT,
        provider TEXT,
        tool TEXT,
        units REAL,
        unit_type TEXT,
        cost_cents INTEGER DEFAULT 0,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_memory (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT,
        source TEXT,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE (user_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harness_approvals (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        job_id BIGINT,
        kind TEXT,
        draft_json TEXT,
        status TEXT DEFAULT 'pending',
        decided_by BIGINT,
        decided_at TEXT,
        created_at TEXT
    )
    """,
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_harness_settings_user ON harness_settings(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_harness_jobs_user ON harness_jobs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_harness_jobs_user_status ON harness_jobs(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_harness_audit_user ON harness_audit(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_harness_costs_user ON harness_costs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_harness_memory_user ON harness_memory(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_harness_approvals_user ON harness_approvals(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_harness_approvals_user_status ON harness_approvals(user_id, status)",
]

# Every existing user gets Drafts Only + unpaused. INSERT ... SELECT ...
# WHERE NOT EXISTS so re-running the migration never duplicates a row
# (and never flips a mode a tenant has since chosen).
_SEED_SQLITE = """
    INSERT OR IGNORE INTO harness_settings
        (user_id, mode, paused, ai_enabled, created_at, updated_at)
    SELECT id, 'drafts_only', 0, 1, ?, ?
    FROM users
    WHERE NOT EXISTS (
        SELECT 1 FROM harness_settings hs WHERE hs.user_id = users.id
    )
"""

_SEED_POSTGRES = """
    INSERT INTO harness_settings
        (user_id, mode, paused, ai_enabled, created_at, updated_at)
    SELECT id, 'drafts_only', 0, 1, ?, ?
    FROM users
    WHERE NOT EXISTS (
        SELECT 1 FROM harness_settings hs WHERE hs.user_id = users.id
    )
    ON CONFLICT (user_id) DO NOTHING
"""


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)
    now = _now()
    conn.execute(_SEED_SQLITE, (now, now))


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)
    now = _now()
    conn.execute(_SEED_POSTGRES, (now, now))


def _missing_tables_sqlite(conn):
    missing = []
    for table in HARNESS_TABLES:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not row:
            missing.append(table)
    return missing


def _missing_tables_postgres(conn):
    missing = []
    for table in HARNESS_TABLES:
        row = conn.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = %s
            LIMIT 1
            """,
            (table,),
        ).fetchone()
        if not row:
            missing.append(table)
    return missing


def verify(conn, dialect):
    if dialect == "postgresql":
        missing = _missing_tables_postgres(conn)
    else:
        missing = _missing_tables_sqlite(conn)
    if missing:
        raise RuntimeError(
            "029_harness_foundation failed: missing table(s): " + ", ".join(missing)
        )


def verify_postgres(conn):
    missing = _missing_tables_postgres(conn)
    if missing:
        raise RuntimeError(
            "029_harness_foundation failed: missing table(s): " + ", ".join(missing)
        )


def verify_sqlite(conn):
    missing = _missing_tables_sqlite(conn)
    if missing:
        raise RuntimeError(
            "029_harness_foundation failed: missing table(s): " + ", ".join(missing)
        )
