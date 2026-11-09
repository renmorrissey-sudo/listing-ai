"""Tests for scripts/backup_postgres.sh (no database needed, except the
retention tests, which stub pg_dump/pg_restore so no real database is
touched).

The script is a thin wrapper around pg_dump. These tests cover its
safety guards, the production fail-safe, retention pruning, and
--dry-run behaviour; the real dump/restore loop is exercised live by
scripts/backup_restore_drill.py (see docs/backup-and-restore.md §6
for the recorded drill results).
"""

import os
import subprocess
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "backup_postgres.sh")


def run_script(args, env_extra=None):
    env = {"PATH": os.environ["PATH"]}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", SCRIPT, *args], env=env, capture_output=True, text=True
    )


def test_dry_run_prints_redacted_command_and_takes_no_backup(tmp_path):
    result = run_script(
        ["--dry-run", "--out-dir", str(tmp_path)],
        {"DATABASE_URL": "postgresql://postgres:SECRET@localhost:5432/topai_dev"},
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    assert "pg_dump" in result.stdout
    # The credential must never appear in output, even in dry-run mode.
    assert "SECRET" not in result.stdout
    assert "SECRET" not in result.stderr
    assert "<DATABASE_URL>" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_missing_database_url_is_an_error():
    result = run_script(["--dry-run"])
    assert result.returncode == 2
    assert "DATABASE_URL is not set" in result.stderr


def test_sqlite_url_is_refused():
    result = run_script(
        ["--dry-run"], {"DATABASE_URL": "sqlite:///real_estate.db"}
    )
    assert result.returncode == 2
    assert "SQLite" in result.stderr


def test_non_postgres_url_is_refused():
    result = run_script(
        ["--dry-run"], {"DATABASE_URL": "mysql://user:pass@host/db"}
    )
    assert result.returncode == 2
    assert "Postgres" in result.stderr


def test_unknown_argument_is_an_error():
    result = run_script(
        ["--explode"], {"DATABASE_URL": "postgresql://postgres@localhost/db"}
    )
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_exits_zero():
    result = run_script(["--help"])
    assert result.returncode == 0
    assert "pg_dump" in result.stdout
    assert "confirm-production" in result.stdout


# --- production fail-safe -----------------------------------------------

def test_remote_host_is_refused_without_confirmation():
    result = run_script(
        ["--dry-run"],
        {"DATABASE_URL": "postgresql://postgres:SECRET@db.railway.internal:5432/railway"},
    )
    assert result.returncode == 2
    assert "PRODUCTION" in result.stderr
    assert "confirm-production" in result.stderr
    # The credential must not leak into the refusal message either.
    assert "SECRET" not in result.stdout
    assert "SECRET" not in result.stderr


def test_remote_host_allowed_with_confirm_flag(tmp_path):
    result = run_script(
        ["--dry-run", "--confirm-production", "--out-dir", str(tmp_path)],
        {"DATABASE_URL": "postgresql://postgres:SECRET@db.railway.internal:5432/railway"},
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    assert "SECRET" not in result.stdout
    assert "SECRET" not in result.stderr


def test_remote_host_allowed_with_confirm_env_var(tmp_path):
    result = run_script(
        ["--dry-run", "--out-dir", str(tmp_path)],
        {
            "DATABASE_URL": "postgresql://postgres@db.railway.internal:5432/railway",
            "BACKUP_CONFIRM_PRODUCTION": "1",
        },
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout


def test_railway_dbname_on_localhost_is_refused_without_confirmation():
    # A `railway connect postgres --tunnel-only` tunnel presents as
    # localhost, but its database is still named `railway` — production.
    result = run_script(
        ["--dry-run"],
        {"DATABASE_URL": "postgresql://postgres@localhost:5432/railway"},
    )
    assert result.returncode == 2
    assert "PRODUCTION" in result.stderr
    assert "confirm-production" in result.stderr


def test_railway_dbname_on_localhost_allowed_with_confirm_flag(tmp_path):
    result = run_script(
        ["--dry-run", "--confirm-production", "--out-dir", str(tmp_path)],
        {"DATABASE_URL": "postgresql://postgres@localhost:5432/railway"},
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout


def test_local_non_railway_db_needs_no_confirmation(tmp_path):
    result = run_script(
        ["--dry-run", "--out-dir", str(tmp_path)],
        {"DATABASE_URL": "postgresql://postgres@127.0.0.1:5432/topai_dev"},
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout


# --- argument / env validation ------------------------------------------

def test_out_dir_missing_value_is_an_error():
    result = run_script(
        ["--out-dir"], {"DATABASE_URL": "postgresql://postgres@localhost/db"}
    )
    assert result.returncode == 2
    assert "--out-dir" in result.stderr


def test_retain_days_must_be_numeric():
    result = run_script(
        ["--retain-days", "soon"],
        {"DATABASE_URL": "postgresql://postgres@localhost/db"},
    )
    assert result.returncode == 2
    assert "retain-days" in result.stderr


# --- retention pruning (stubbed pg_dump / pg_restore) --------------------

def _stub_bin(tmp_path):
    """Fake pg_dump/pg_restore so a 'real' run touches no database."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pg_dump = bindir / "pg_dump"
    pg_dump.write_text(
        "#!/usr/bin/env bash\n"
        "for a in \"$@\"; do case \"$a\" in --file=*) f=\"${a#--file=}\"; echo DUMMY > \"$f\";; esac; done\n"
    )
    pg_restore = bindir / "pg_restore"
    pg_restore.write_text(
        "#!/usr/bin/env bash\n"
        "echo '1; 0 0 TABLE public users postgres'\n"
    )
    pg_dump.chmod(0o755)
    pg_restore.chmod(0o755)
    return str(bindir)


def _old_dump(out_dir, name, days_old):
    path = out_dir / name
    path.write_bytes(b"old")
    old = time.time() - days_old * 86400
    os.utime(path, (old, old))
    return path


def test_retention_prunes_old_dumps_but_keeps_newest(tmp_path):
    bindir = _stub_bin(tmp_path)
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    old1 = _old_dump(out_dir, "backup-20200101-000000.dump", 30)
    old2 = _old_dump(out_dir, "backup-20200102-000000.dump", 10)
    env = {
        "PATH": bindir + os.pathsep + os.environ["PATH"],
        "DATABASE_URL": "postgresql://postgres@localhost:5432/topai_dev",
    }
    result = run_script(
        ["--out-dir", str(out_dir), "--retain-days", "7"], env
    )
    assert result.returncode == 0
    assert "Backup complete." in result.stdout
    assert "pruned 2 dump(s)" in result.stdout
    assert not old1.exists()
    assert not old2.exists()
    # The dump just taken is the newest and must survive pruning.
    remaining = sorted(p.name for p in out_dir.iterdir())
    assert len(remaining) == 1
    assert remaining[0].startswith("backup-")


def test_no_pruning_by_default(tmp_path):
    bindir = _stub_bin(tmp_path)
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    old1 = _old_dump(out_dir, "backup-20200101-000000.dump", 30)
    env = {
        "PATH": bindir + os.pathsep + os.environ["PATH"],
        "DATABASE_URL": "postgresql://postgres@localhost:5432/topai_dev",
    }
    result = run_script(["--out-dir", str(out_dir)], env)
    assert result.returncode == 0
    assert "Retention:" not in result.stdout
    assert old1.exists()
