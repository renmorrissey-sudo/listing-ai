"""Baseline schema for TopAI CRM and tools.

Derived from the live application tables used in db.py / crm_db.py.
Additive only. Never drops tables or truncates data.
"""

from __future__ import annotations

import sys

VERSION = "001_baseline"

# Complete application schema (no separate accounts/tenants/subscriptions/
# sms_conversations tables — those concepts live on users / leads / sms_messages).
POSTGRES_TABLES = {
    "schema_migrations": """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
    "users": """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            stripe_customer_id TEXT,
            subscription_status TEXT NOT NULL DEFAULT 'none',
            subscription_id TEXT,
            created_at TEXT NOT NULL,
            role TEXT DEFAULT 'agent',
            agent_name TEXT,
            brokerage_name TEXT,
            company_name TEXT
        )
        """,
    "voice_personas": """
        CREATE TABLE IF NOT EXISTS voice_personas (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            name TEXT NOT NULL,
            persona_type TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tone TEXT NOT NULL DEFAULT 'professional',
            goal TEXT NOT NULL,
            objection_handling_notes TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """,
    "voice_calls": """
        CREATE TABLE IF NOT EXISTS voice_calls (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            persona_id BIGINT,
            provider TEXT NOT NULL,
            provider_call_id TEXT,
            direction TEXT NOT NULL,
            lead_name TEXT,
            phone_number TEXT NOT NULL,
            lead_type TEXT,
            property_interest TEXT,
            desired_outcome TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            outcome TEXT,
            appointment_requested INTEGER NOT NULL DEFAULT 0,
            transcript TEXT,
            summary TEXT,
            recording_url TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
    "tool_usage": """
        CREATE TABLE IF NOT EXISTS tool_usage (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            tool_key TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'generated',
            metadata TEXT,
            created_at TEXT NOT NULL
        )
        """,
    "sms_messages": """
        CREATE TABLE IF NOT EXISTS sms_messages (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            persona_id BIGINT,
            lead_id BIGINT,
            provider TEXT NOT NULL DEFAULT 'twilio',
            provider_message_id TEXT,
            direction TEXT NOT NULL DEFAULT 'outbound',
            lead_name TEXT,
            phone_number TEXT NOT NULL,
            lead_type TEXT,
            property_interest TEXT,
            desired_outcome TEXT,
            notes TEXT,
            message_body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            error_message TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            consent_status TEXT DEFAULT 'unknown',
            opt_out_status TEXT DEFAULT 'active',
            approved_by_user_id BIGINT,
            consent_source TEXT
        )
        """,
    "leads": """
        CREATE TABLE IF NOT EXISTS leads (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            name TEXT,
            phone_number TEXT NOT NULL,
            lead_type TEXT,
            property_interest TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            source TEXT NOT NULL DEFAULT 'sms',
            notes TEXT,
            next_action TEXT,
            follow_up_at TEXT,
            last_inbound_at TEXT,
            last_outbound_at TEXT,
            consent_status TEXT NOT NULL DEFAULT 'unknown',
            opt_out_status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            priority TEXT DEFAULT 'normal',
            assigned_user_id BIGINT,
            next_follow_up_at TEXT,
            follow_up_reason TEXT,
            follow_up_priority TEXT DEFAULT 'normal',
            follow_up_completed_at TEXT,
            follow_up_created_by BIGINT,
            UNIQUE (user_id, phone_number)
        )
        """,
    "lead_follow_ups": """
        CREATE TABLE IF NOT EXISTS lead_follow_ups (
            id BIGSERIAL PRIMARY KEY,
            lead_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            due_at TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            priority TEXT DEFAULT 'normal',
            created_by BIGINT,
            completed_at TEXT
        )
        """,
    "lead_insights": """
        CREATE TABLE IF NOT EXISTS lead_insights (
            id BIGSERIAL PRIMARY KEY,
            lead_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            inbound_message_id BIGINT,
            suggested_message_id BIGINT,
            summary TEXT,
            intent TEXT,
            next_best_step TEXT,
            recommended_action TEXT,
            suggested_reply TEXT,
            home_value_pitch TEXT,
            confidence_score DOUBLE PRECISION,
            requires_manual_review INTEGER NOT NULL DEFAULT 0,
            escalation_topics TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            raw_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
    "lead_activities": """
        CREATE TABLE IF NOT EXISTS lead_activities (
            id BIGSERIAL PRIMARY KEY,
            lead_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            actor_user_id BIGINT,
            event_type TEXT NOT NULL,
            summary TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
    "tasks": """
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            lead_id BIGINT,
            assigned_user_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            due_at TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'open',
            task_type TEXT NOT NULL DEFAULT 'general_follow_up',
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    "appointments": """
        CREATE TABLE IF NOT EXISTS appointments (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            lead_id BIGINT NOT NULL,
            appointment_type TEXT NOT NULL DEFAULT 'phone_call',
            start_at TEXT NOT NULL,
            end_at TEXT,
            location TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            outcome TEXT,
            outcome_notes TEXT,
            next_action TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    "needs_attention": """
        CREATE TABLE IF NOT EXISTS needs_attention (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            lead_id BIGINT,
            reason_code TEXT NOT NULL,
            reason_text TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            source_ref_type TEXT,
            source_ref_id BIGINT,
            status TEXT NOT NULL DEFAULT 'open',
            resolution_reason TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by BIGINT
        )
        """,
    "notifications": """
        CREATE TABLE IF NOT EXISTS notifications (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            link TEXT,
            lead_id BIGINT,
            read_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
}

# Dependency order: migrations bookkeeping, then users, then dependents.
POSTGRES_TABLE_ORDER = [
    "schema_migrations",
    "users",
    "voice_personas",
    "voice_calls",
    "tool_usage",
    "leads",
    "sms_messages",
    "lead_follow_ups",
    "lead_insights",
    "lead_activities",
    "tasks",
    "appointments",
    "needs_attention",
    "notifications",
]

POSTGRES_INDEXES = [
    ("idx_sms_messages_user_created", "CREATE INDEX IF NOT EXISTS idx_sms_messages_user_created ON sms_messages (user_id, created_at)"),
    ("idx_sms_messages_provider_id", "CREATE INDEX IF NOT EXISTS idx_sms_messages_provider_id ON sms_messages (provider_message_id)"),
    ("idx_sms_messages_lead_created", "CREATE INDEX IF NOT EXISTS idx_sms_messages_lead_created ON sms_messages (lead_id, created_at)"),
    ("idx_leads_user_updated", "CREATE INDEX IF NOT EXISTS idx_leads_user_updated ON leads (user_id, updated_at)"),
    ("idx_lead_follow_ups_user_due", "CREATE INDEX IF NOT EXISTS idx_lead_follow_ups_user_due ON lead_follow_ups (user_id, due_at)"),
    ("idx_lead_insights_user_status", "CREATE INDEX IF NOT EXISTS idx_lead_insights_user_status ON lead_insights (user_id, status, created_at)"),
    ("idx_tool_usage_user_created", "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_created ON tool_usage (user_id, created_at)"),
    ("idx_tool_usage_user_tool", "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_tool ON tool_usage (user_id, tool_key)"),
    ("idx_lead_activities_lead", "CREATE INDEX IF NOT EXISTS idx_lead_activities_lead ON lead_activities (lead_id, created_at)"),
    ("idx_tasks_user_due", "CREATE INDEX IF NOT EXISTS idx_tasks_user_due ON tasks (user_id, due_at)"),
    ("idx_tasks_user_status", "CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks (user_id, status)"),
    ("idx_appointments_user_start", "CREATE INDEX IF NOT EXISTS idx_appointments_user_start ON appointments (user_id, start_at)"),
    ("idx_needs_attention_user_status", "CREATE INDEX IF NOT EXISTS idx_needs_attention_user_status ON needs_attention (user_id, status, created_at)"),
    ("idx_notifications_user", "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, created_at)"),
]


def upgrade_postgres(conn):
    """Create the full application schema using raw psycopg DDL + autocommit."""
    from migrations.pg_ddl import pg_execute, pg_table_exists

    raw = conn._raw
    previous_autocommit = raw.autocommit
    raw.autocommit = True
    created = []
    try:
        for table in POSTGRES_TABLE_ORDER:
            ddl = POSTGRES_TABLES[table]
            print(f"001_baseline: CREATE TABLE {table}", file=sys.stderr)
            pg_execute(conn, ddl)
            if not pg_table_exists(conn, table):
                raise RuntimeError(
                    f"001_baseline failed: CREATE TABLE {table} did not create a "
                    f"public base table (to_regclass/pg_class check failed)."
                )
            created.append(table)

        for index_name, ddl in POSTGRES_INDEXES:
            print(f"001_baseline: CREATE INDEX {index_name}", file=sys.stderr)
            pg_execute(conn, ddl)
    finally:
        raw.autocommit = previous_autocommit

    print(
        "001_baseline: created tables: " + ", ".join(created),
        file=sys.stderr,
    )


def upgrade_sqlite(conn):
    """SQLite baseline for local development/test only."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            stripe_customer_id TEXT,
            subscription_status TEXT NOT NULL DEFAULT 'none',
            subscription_id TEXT,
            created_at TEXT NOT NULL,
            role TEXT DEFAULT 'agent',
            agent_name TEXT,
            brokerage_name TEXT,
            company_name TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS voice_personas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            persona_type TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tone TEXT NOT NULL DEFAULT 'professional',
            goal TEXT NOT NULL,
            objection_handling_notes TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS voice_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            persona_id INTEGER,
            provider TEXT NOT NULL,
            provider_call_id TEXT,
            direction TEXT NOT NULL,
            lead_name TEXT,
            phone_number TEXT NOT NULL,
            lead_type TEXT,
            property_interest TEXT,
            desired_outcome TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            outcome TEXT,
            appointment_requested INTEGER NOT NULL DEFAULT 0,
            transcript TEXT,
            summary TEXT,
            recording_url TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tool_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tool_key TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'generated',
            metadata TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sms_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            persona_id INTEGER,
            lead_id INTEGER,
            provider TEXT NOT NULL DEFAULT 'twilio',
            provider_message_id TEXT,
            direction TEXT NOT NULL DEFAULT 'outbound',
            lead_name TEXT,
            phone_number TEXT NOT NULL,
            lead_type TEXT,
            property_interest TEXT,
            desired_outcome TEXT,
            notes TEXT,
            message_body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            error_message TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            consent_status TEXT DEFAULT 'unknown',
            opt_out_status TEXT DEFAULT 'active',
            approved_by_user_id INTEGER,
            consent_source TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            phone_number TEXT NOT NULL,
            lead_type TEXT,
            property_interest TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            source TEXT NOT NULL DEFAULT 'sms',
            notes TEXT,
            next_action TEXT,
            follow_up_at TEXT,
            last_inbound_at TEXT,
            last_outbound_at TEXT,
            consent_status TEXT NOT NULL DEFAULT 'unknown',
            opt_out_status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            priority TEXT DEFAULT 'normal',
            assigned_user_id INTEGER,
            next_follow_up_at TEXT,
            follow_up_reason TEXT,
            follow_up_priority TEXT DEFAULT 'normal',
            follow_up_completed_at TEXT,
            follow_up_created_by INTEGER,
            UNIQUE(user_id, phone_number)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lead_follow_ups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            due_at TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            priority TEXT DEFAULT 'normal',
            created_by INTEGER,
            completed_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lead_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            inbound_message_id INTEGER,
            suggested_message_id INTEGER,
            summary TEXT,
            intent TEXT,
            next_best_step TEXT,
            recommended_action TEXT,
            suggested_reply TEXT,
            home_value_pitch TEXT,
            confidence_score REAL,
            requires_manual_review INTEGER NOT NULL DEFAULT 0,
            escalation_topics TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            raw_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lead_activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            actor_user_id INTEGER,
            event_type TEXT NOT NULL,
            summary TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lead_id INTEGER,
            assigned_user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            due_at TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'open',
            task_type TEXT NOT NULL DEFAULT 'general_follow_up',
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lead_id INTEGER NOT NULL,
            appointment_type TEXT NOT NULL DEFAULT 'phone_call',
            start_at TEXT NOT NULL,
            end_at TEXT,
            location TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            outcome TEXT,
            outcome_notes TEXT,
            next_action TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS needs_attention (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lead_id INTEGER,
            reason_code TEXT NOT NULL,
            reason_text TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            source_ref_type TEXT,
            source_ref_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            resolution_reason TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            link TEXT,
            lead_id INTEGER,
            read_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sms_messages_user_created ON sms_messages(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_sms_messages_provider_id ON sms_messages(provider_message_id)",
        "CREATE INDEX IF NOT EXISTS idx_sms_messages_lead_created ON sms_messages(lead_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_leads_user_updated ON leads(user_id, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_lead_follow_ups_user_due ON lead_follow_ups(user_id, due_at)",
        "CREATE INDEX IF NOT EXISTS idx_lead_insights_user_status ON lead_insights(user_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_created ON tool_usage(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_tool ON tool_usage(user_id, tool_key)",
        "CREATE INDEX IF NOT EXISTS idx_lead_activities_lead ON lead_activities(lead_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_due ON tasks(user_id, due_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_appointments_user_start ON appointments(user_id, start_at)",
        "CREATE INDEX IF NOT EXISTS idx_needs_attention_user_status ON needs_attention(user_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at)",
    ]
    for stmt in statements:
        conn.execute(stmt)
