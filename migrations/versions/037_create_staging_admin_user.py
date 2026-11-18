"""Create the staging admin account for Ren (037).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main. Staging has no self-service
registration (private-beta gate, and signup requires Stripe billing,
which staging does not configure) and no outbound mail (no SendGrid /
SMTP), so a staging account cannot be created or password-reset
through the UI. This migration inserts the admin account directly;
the password is a one-time bootstrap credential handed to Ren
(staging-only, throwaway). Idempotent: no-op when the email already
exists. No existing rows are modified.

Rollback: none needed — delete the user row to remove the account.
"""

VERSION = "037_create_staging_admin_user"

EMAIL = "ren.morrissey@gmail.com"
PASSWORD_HASH = "scrypt:32768:8:1$xQya1B3lH6wmRzGN$a1e4c3227a6e633f82c3d4292f207c2681170a92ac74f429fc506d41acd3a5c5f0314fd9d92c340be1a8fe512e558b75b8bf7575ec4705032895e52907405e4b"
CREATED_AT = "2026-11-18T15:48:44.090897+00:00"


def upgrade_postgres(conn):
    conn.execute(
        "INSERT INTO users (email, password_hash, created_at, password_set, session_version) "
        "SELECT '" + EMAIL + "', '" + PASSWORD_HASH + "', '" + CREATED_AT + "', TRUE, 1 "
        "WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = '" + EMAIL + "')"
    )


def upgrade_sqlite(conn):
    # Staging-only account; test databases do not need it. No-op so the
    # local test suite gains no real user row.
    return None


def _verify_postgres(conn):
    row = conn.execute(
        "SELECT 1 FROM users WHERE email = '" + EMAIL + "'"
    ).fetchone()
    if row is None:
        raise RuntimeError("037_create_staging_admin_user failed: user missing")


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
