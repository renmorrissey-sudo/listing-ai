import importlib
import sqlite3


def test_engaged_backfill_only_uses_substantive_open_exchanges():
    migration = importlib.import_module(
        "migrations.versions.024_engaged_lead_status"
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            opt_out_status TEXT,
            sms_consent_status TEXT
        );
        CREATE TABLE sms_messages (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            lead_id INTEGER,
            direction TEXT,
            status TEXT,
            message_body TEXT
        );
        CREATE TABLE voice_calls (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            lead_id INTEGER,
            status TEXT
        );

        INSERT INTO leads VALUES (1, 10, 'contacted', 'active', 'verified');
        INSERT INTO leads VALUES (2, 10, 'contacted', 'active', 'verified');
        INSERT INTO leads VALUES (3, 10, 'qualified', 'active', 'verified');
        INSERT INTO leads VALUES (4, 10, 'nurture', 'active', 'verified');
        INSERT INTO leads VALUES (5, 10, 'replied', 'active', 'verified');
        INSERT INTO sms_messages VALUES (1, 10, 1, 'inbound', 'received', 'I am interested');
        INSERT INTO sms_messages VALUES (2, 10, 2, 'inbound', 'received', 'HELP');
        INSERT INTO sms_messages VALUES (3, 10, 3, 'inbound', 'received', 'Let us talk');
        INSERT INTO voice_calls VALUES (1, 10, 4, 'completed');
        """
    )

    migration.upgrade_sqlite(conn)
    statuses = dict(conn.execute("SELECT id, status FROM leads").fetchall())

    assert statuses == {
        1: "engaged",
        2: "contacted",
        3: "qualified",
        4: "engaged",
        5: "engaged",
    }
