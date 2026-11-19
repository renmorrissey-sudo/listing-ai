"""Reset the staging admin account password for Ren (044).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main. Ren's sign-in as
ren.morrissey@gmail.com is failing with "Invalid email or password"
(he was trying the wrong password; the original 037 one never matched
its hash). This migration sets the password hash unconditionally to a
fresh, locally verified hash (check_password_hash confirmed True before
the file was written), so it fixes the account regardless of what the
current stored hash is. The password is a one-time bootstrap credential
handed to Ren (staging-only, throwaway). Idempotent: re-running it sets
the same hash again. No other rows are modified.

Rollback: none needed — delete the user row to remove the account.
"""

VERSION = "044_reset_staging_admin_password"

EMAIL = "ren.morrissey@gmail.com"
PASSWORD_HASH = "scrypt:32768:8:1$JWijHHg2ouWtxJgH$51a1f4b7fbc630bdc35456218574d72950b6b2b7e2748b9f40daad36ff9e2753da64c2bc9f12b4a58f9272d2528ac5aa248f388f7aade872adbc110ef57258f3"


def upgrade_postgres(conn):
    conn.execute(
        "UPDATE users SET password_hash = '" + PASSWORD_HASH + "', "
        "password_set = TRUE, session_version = session_version + 1 "
        "WHERE email = '" + EMAIL + "'"
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
        raise RuntimeError("044_reset_staging_admin_password failed: user missing")


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
