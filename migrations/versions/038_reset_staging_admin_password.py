"""Reset the staging admin account password for Ren (038).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main. Migration 037 inserted the
account, but the bootstrap password handed to Ren did not match its
hash (verified: the 037 hash fails check_password_hash for the
relayed password), so sign-in kept failing. This migration sets the
password hash unconditionally to a fresh, locally verified hash, so
it fixes the account whether 037 inserted it or it already existed.
The password is a one-time bootstrap credential handed to Ren
(staging-only, throwaway). Idempotent: re-running it sets the same
hash again. No other rows are modified.

Rollback: none needed — delete the user row to remove the account.
"""

VERSION = "038_reset_staging_admin_password"

EMAIL = "ren.morrissey@gmail.com"
PASSWORD_HASH = "scrypt:32768:8:1$DjVbLiIAE9hOuHGa$e219db8be272a45228ae56e1e067b5b0d39467fc17cab9a4bf3db8b0837c482e932dc0203770462310caab777fddfeaec6d72614d3a2b9d09c8589d02700ea09"


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
        raise RuntimeError("038_reset_staging_admin_password failed: user missing")


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
