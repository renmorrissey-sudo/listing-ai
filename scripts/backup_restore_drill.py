#!/usr/bin/env python3
"""Backup/restore drill for TopAI Postgres — isolated local Postgres only.

RELEASE VARIANT (topai-launch-readiness-release, 2026-11-09): this
release contains no schema migrations, so the drill runs migrations
001..032 and verifies that the 033 ledger columns and the 034 claim
index are ABSENT by design on the restored DB. The 033/034 files remain
in git history (branch topai-launch-readiness-final, commit e93cf7e)
for a future migration-bearing release, when this script's
EXPECTED_MIGRATIONS returns to 34.

Runs the full loop end to end and proves the dump actually restores:

  1. Create a fresh source database on LOCAL Postgres.
  2. Run the repo's full migration set (001..032) against it via
     `python -m migrations.runner` (APP_ENV=development so the runner's
     production/staging guard against local hosts does not block it;
     the code path is the same).
  3. Seed synthetic harness + app data (2 tenants, harness settings /
     jobs / audit / costs / approvals / memory, tenant_sms_senders,
     leads, tasks, sms_messages).
  4. Snapshot row counts + per-table content hashes + migration stamps.
  5. pg_dump --format=custom --no-owner to a file; record size + SHA-256.
  6. Restore into a FRESH database with pg_restore.
  7. Re-snapshot the restored DB and compare row-for-row (counts,
     content hashes, migration stamps, correlation IDs, tenant-scoped
     uniqueness). Re-run the migration runner against the restored DB
     (must be a no-op: every migration already applied).
  8. Print PASS/FAIL with timings and exit non-zero on any mismatch.

SAFETY — this script refuses to run unless BOTH of these hold:
  * the Postgres host is localhost / 127.0.0.1 / ::1, and
  * both database names end in `_drill` (scratch names only).
It never touches Railway, never reads a real DATABASE_URL, and never
runs against a database that could hold real data.

Usage (Postgres 16 running locally, superuser `postgres`, trust auth):
  .venv/bin/python scripts/backup_restore_drill.py [--keep] [--dump-file PATH]
  --keep       do not drop the source/restore databases afterwards
               (default: drop both; the dump file is always kept)
  --dump-file  where to write the dump (default: /tmp/topai-backup-drill/backup-<stamp>.dump)

Requires pg_dump / pg_restore / createdb / dropdb on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"
SOURCE_DB = "topai_backup_drill_source_drill"
RESTORE_DB = "topai_backup_drill_restore_drill"

# Tables whose full content is hashed row-for-row (ordered by id).
HASH_TABLES = [
    "users",
    "harness_settings",
    "harness_jobs",
    "harness_audit",
    "harness_costs",
    "harness_memory",
    "harness_approvals",
    "tenant_sms_senders",
    "leads",
    "tasks",
    "sms_messages",
]

# Tables whose row counts are compared (content hash covered above for
# the important ones; counts for the rest of the migrated schema).
COUNT_TABLES = [
    "schema_migrations",
    "voice_personas",
    "voice_calls",
    "tool_usage",
    "lead_follow_ups",
    "lead_insights",
    "lead_activities",
    "appointments",
    "needs_attention",
    "notifications",
]

EXPECTED_MIGRATIONS = 32  # 001..032 (release variant: 033/034 excluded)


def run(cmd, env=None, check=True):
    # CLI tools (createdb/dropdb/pg_dump/pg_restore) default to the OS
    # user over the Unix socket; force the postgres superuser over TCP
    # localhost so the drill works regardless of who runs it.
    child_env = {"PGHOST": "localhost", "PGUSER": "postgres",
                 "PATH": os.environ.get("PATH", "")}
    if env:
        child_env.update(env)
    print(f"  $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, env=child_env, capture_output=True, text=True)
    if result.stdout.strip():
        print("    " + result.stdout.strip().replace("\n", "\n    "), flush=True)
    if result.stderr.strip():
        print("    [stderr] " + result.stderr.strip().replace("\n", "\n    [stderr] "), flush=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def db_url(dbname):
    return f"postgresql://postgres@localhost:5432/{dbname}"


def snapshot(conn):
    """Row counts + content hashes + migration stamps for one database."""
    counts = {}
    hashes = {}
    for table in HASH_TABLES + COUNT_TABLES:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        counts[table] = int(row["c"])
    for table in HASH_TABLES:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        blob = json.dumps([dict(r) for r in rows], sort_keys=True, default=str)
        hashes[table] = hashlib.sha256(blob.encode()).hexdigest()
    stamps = [r["version"] for r in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
    table_count = conn.execute(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'").fetchone()["c"]
    return {"counts": counts, "hashes": hashes, "stamps": stamps,
            "table_count": int(table_count)}


def seed(conn):
    """Synthetic data: 2 tenants, shared platform number, scoped keys."""
    now = datetime.now(timezone.utc).isoformat()
    corr_a = uuid.uuid4().hex
    corr_b = uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash, subscription_status, created_at) "
            "VALUES ('tenant-a@example.com', 'x', 'active', %s) RETURNING id", (now,))
        user_a = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO users (email, password_hash, subscription_status, created_at) "
            "VALUES ('tenant-b@example.com', 'x', 'active', %s) RETURNING id", (now,))
        user_b = cur.fetchone()["id"]
        # harness_settings: migration 029 seeds only users that existed at
        # migration time; these users were created after, so seed explicitly.
        cur.execute(
            "INSERT INTO harness_settings (user_id, mode, paused, ai_enabled, created_at, updated_at) "
            "VALUES (%s, 'drafts_only', 0, 1, %s, %s)", (user_a, now, now))
        cur.execute(
            "INSERT INTO harness_settings (user_id, mode, paused, ai_enabled, created_at, updated_at) "
            "VALUES (%s, 'supervised', 0, 1, %s, %s)", (user_b, now, now))
        # harness_jobs: same idempotency key string for BOTH tenants —
        # legal only because 030 scopes uniqueness to (user_id, key).
        jobs = [
            (user_a, "sms_auto_reply", "sms-auto-reply-1", "succeeded", corr_a),
            (user_a, "scheduled_follow_up", "followup-1", "pending", uuid.uuid4().hex),
            (user_a, "sms_auto_reply", "sms-auto-reply-2", "failed", uuid.uuid4().hex),
            (user_a, "execute_approval", "approval-exec-1", "succeeded", uuid.uuid4().hex),
            (user_a, "sms_auto_reply", "sms-auto-reply-3", "dead_letter", uuid.uuid4().hex),
            (user_b, "sms_auto_reply", "sms-auto-reply-1", "pending", corr_b),
            (user_b, "scheduled_follow_up", "followup-1", "succeeded", uuid.uuid4().hex),
            (user_b, "sms_auto_reply", "sms-auto-reply-9", "succeeded", uuid.uuid4().hex),
        ]
        job_ids = {}
        for user_id, kind, key, status, corr in jobs:
            cur.execute(
                "INSERT INTO harness_jobs (user_id, kind, payload_json, idempotency_key, "
                "status, attempts, correlation_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s) RETURNING id",
                (user_id, kind, json.dumps({"lead_id": 1}), key, status, corr, now, now))
            job_ids[(user_id, key)] = cur.fetchone()["id"]
        # harness_audit: one row per job outcome, correlation_id chained.
        corr_by_key = {(u, k): c for (u, _kind, k, _status, c) in jobs}
        for (user_id, key), job_id in job_ids.items():
            corr = corr_by_key[(user_id, key)]
            cur.execute(
                "INSERT INTO harness_audit (user_id, actor, action, tool, target_type, "
                "target_id, mode, gate_decision, gate_reason, cost_cents, correlation_id, created_at) "
                "VALUES (%s, 'worker', 'job_completed', 'send_sms', 'harness_job', %s, "
                "'drafts_only', 'DRAFT', 'drill seed', 12, %s, %s)",
                (user_id, job_id, corr, now))
        # harness_costs: Anthropic token usage rows.
        for user_id in (user_a, user_b):
            cur.execute(
                "INSERT INTO harness_costs (user_id, period, provider, tool, units, unit_type, cost_cents, created_at) "
                "VALUES (%s, '2026-11', 'anthropic', 'ask_topai', 1500, 'tokens', 45, %s)",
                (user_id, now))
        # harness_approvals: one pending, one approved.
        cur.execute(
            "INSERT INTO harness_approvals (user_id, job_id, kind, draft_json, status, created_at) "
            "VALUES (%s, %s, 'send_sms', %s, 'pending', %s)",
            (user_a, job_ids[(user_a, "sms-auto-reply-1")], json.dumps({"text": "Hi!"}), now))
        cur.execute(
            "INSERT INTO harness_approvals (user_id, job_id, kind, draft_json, status, decided_by, decided_at, created_at) "
            "VALUES (%s, %s, 'send_sms', %s, 'approved', %s, %s, %s)",
            (user_b, job_ids[(user_b, "sms-auto-reply-1")], json.dumps({"text": "Hi!"}), user_b, now, now))
        # harness_memory.
        cur.execute(
            "INSERT INTO harness_memory (user_id, key, value_json, source, created_at, updated_at) "
            "VALUES (%s, 'lead_pref', %s, 'drill', %s, %s)",
            (user_a, json.dumps({"tone": "short"}), now, now))
        # tenant_sms_senders: BOTH tenants share the platform number
        # +18888210810 — legal only because 032 scopes uniqueness to
        # (user_id, sender_number). Tenant A also has its own number.
        for user_id in (user_a, user_b):
            cur.execute(
                "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
                "sms_enabled, registration_status, created_at, updated_at) "
                "VALUES (%s, 'telnyx', '+18888210810', TRUE, 'verified', %s, %s)",
                (user_id, now, now))
        cur.execute(
            "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
            "sms_enabled, registration_status, created_at, updated_at) "
            "VALUES (%s, 'telnyx', '+13035550100', TRUE, 'verified', %s, %s)",
            (user_a, now, now))
        # App tables: leads / tasks / sms_messages, 2 per tenant.
        for user_id in (user_a, user_b):
            for i in (1, 2):
                cur.execute(
                    "INSERT INTO leads (user_id, name, phone_number, status, source, created_at, updated_at) "
                    "VALUES (%s, %s, %s, 'new', 'sms', %s, %s)",
                    (user_id, f"Lead {i}", f"+1303555010{i}", now, now))
                cur.execute(
                    "INSERT INTO tasks (user_id, assigned_user_id, title, status, created_at, updated_at) "
                    "VALUES (%s, %s, %s, 'open', %s, %s)",
                    (user_id, user_id, f"Follow up {i}", now, now))
                cur.execute(
                    "INSERT INTO sms_messages (user_id, phone_number, message_body, status, created_at) "
                    "VALUES (%s, %s, %s, 'sent', %s)",
                    (user_id, f"+1303555010{i}", f"Hello lead {i}", now))
    conn.commit()
    return {"user_a": user_a, "user_b": user_b, "corr_a": corr_a, "corr_b": corr_b}


def check_tenant_isolation(conn):
    """Post-restore invariants: shared number OK, duplicates rejected."""
    errors = []
    shared = conn.execute(
        "SELECT COUNT(*) AS c FROM tenant_sms_senders WHERE sender_number = '+18888210810'"
    ).fetchone()["c"]
    if int(shared) != 2:
        errors.append(f"shared platform number +18888210810 has {shared} rows, expected 2")
    # Same idempotency key for both tenants must exist (030 scoping).
    keys = conn.execute(
        "SELECT COUNT(*) AS c FROM harness_jobs WHERE idempotency_key = 'sms-auto-reply-1'"
    ).fetchone()["c"]
    if int(keys) != 2:
        errors.append(f"idempotency key 'sms-auto-reply-1' has {keys} rows, expected 2 (one per tenant)")
    # Duplicate (user_id, sender_number) must be rejected (032 scoping).
    user_a = conn.execute(
        "SELECT id FROM users WHERE email = 'tenant-a@example.com'").fetchone()["id"]
    try:
        conn.execute(
            "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
            "sms_enabled, registration_status, created_at, updated_at) "
            "VALUES (%s, 'telnyx', '+18888210810', TRUE, 'verified', 'x', 'x')", (user_a,))
        conn.commit()
        errors.append("duplicate (user_id, sender_number) was ACCEPTED — 032 uniqueness lost")
    except Exception:
        conn.rollback()
    # Correlation IDs survived the round trip.
    nulls = conn.execute(
        "SELECT COUNT(*) AS c FROM harness_jobs WHERE correlation_id IS NULL").fetchone()["c"]
    if int(nulls) != 0:
        errors.append(f"{nulls} harness_jobs rows have NULL correlation_id after restore")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="do not drop the drill databases afterwards")
    parser.add_argument("--dump-file", default=None,
                        help="dump path (default: /tmp/topai-backup-drill/backup-<stamp>.dump)")
    args = parser.parse_args()

    t0 = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dump_file = args.dump_file or f"/tmp/topai-backup-drill/backup-{stamp}.dump"
    os.makedirs(os.path.dirname(dump_file), exist_ok=True)

    failures = []
    print("== TopAI backup/restore drill — LOCAL Postgres only ==")
    print(f"   source db:  {SOURCE_DB}")
    print(f"   restore db: {RESTORE_DB}")
    print(f"   dump file:  {dump_file}")

    # --- safety: local host only, scratch names only -------------------
    for name in (SOURCE_DB, RESTORE_DB):
        assert name.endswith("_drill"), name
    print("[1/8] safety check: local host, scratch db names — OK")

    # --- create source db ----------------------------------------------
    print("[2/8] creating source database")
    run(["dropdb", "--if-exists", SOURCE_DB], check=False)
    run(["createdb", SOURCE_DB])
    run(["dropdb", "--if-exists", RESTORE_DB], check=False)

    # --- run the repo's full migration set ------------------------------
    print("[3/8] running migrations 001..032 (python -m migrations.runner)")
    t_mig = time.time()
    env = dict(os.environ)
    env["APP_ENV"] = "development"  # local host: production/staging guard refuses localhost
    env["DATABASE_URL"] = db_url(SOURCE_DB)
    env.pop("DATABASE_PATH", None)
    run([sys.executable, "-m", "migrations.runner"], env=env)
    print(f"    migrations took {time.time() - t_mig:.1f}s")

    # --- seed + snapshot -------------------------------------------------
    print("[4/8] seeding synthetic data + snapshotting source")
    src = psycopg.connect(db_url(SOURCE_DB), row_factory=dict_row)
    seed_info = seed(src)
    src_snap = snapshot(src)
    src.close()
    total_rows = sum(src_snap["counts"].values())
    print(f"    source: {src_snap['table_count']} tables, "
          f"{len(src_snap['stamps'])} migration stamps, {total_rows} rows in tracked tables")
    if len(src_snap["stamps"]) != EXPECTED_MIGRATIONS:
        failures.append(f"migration stamps: {len(src_snap['stamps'])}, expected {EXPECTED_MIGRATIONS}")
    if src_snap["stamps"] and src_snap["stamps"][-1] != "032_tenant_sms_senders_scoped_unique":
        failures.append(f"latest stamp is {src_snap['stamps'][-1]}, expected 032_...")

    # --- pg_dump ----------------------------------------------------------
    print("[5/8] pg_dump (custom format, --no-owner)")
    t_dump = time.time()
    run(["pg_dump", db_url(SOURCE_DB), "--format=custom", "--no-owner",
         f"--file={dump_file}"])
    dump_seconds = time.time() - t_dump
    dump_size = os.path.getsize(dump_file)
    dump_sha = hashlib.sha256(open(dump_file, "rb").read()).hexdigest()
    print(f"    dump: {dump_size} bytes, sha256 {dump_sha}, took {dump_seconds:.1f}s")
    listing = run(["pg_restore", "--list", dump_file], check=False)
    table_entries = sum(1 for line in listing.stdout.splitlines() if " TABLE " in line)
    print(f"    pg_restore --list: {table_entries} TABLE entries")
    if dump_size == 0:
        failures.append("dump file is empty")
    if table_entries < len(HASH_TABLES):
        failures.append(f"dump lists only {table_entries} tables, expected >= {len(HASH_TABLES)}")

    # --- pg_restore into a fresh database ---------------------------------
    print("[6/8] restoring into a fresh database")
    t_restore = time.time()
    run(["createdb", RESTORE_DB])
    run(["pg_restore", "--no-owner", "--exit-on-error",
         f"--dbname={db_url(RESTORE_DB)}", dump_file])
    restore_seconds = time.time() - t_restore
    print(f"    restore took {restore_seconds:.1f}s")

    # --- compare -----------------------------------------------------------
    print("[7/8] comparing restored database against source snapshot")
    dst = psycopg.connect(db_url(RESTORE_DB), row_factory=dict_row)
    dst_snap = snapshot(dst)
    for table, count in src_snap["counts"].items():
        if dst_snap["counts"].get(table) != count:
            failures.append(
                f"row count mismatch {table}: source={count} restored={dst_snap['counts'].get(table)}")
    for table, digest in src_snap["hashes"].items():
        if dst_snap["hashes"].get(table) != digest:
            failures.append(f"content hash mismatch {table} (rows differ after restore)")
    if dst_snap["stamps"] != src_snap["stamps"]:
        failures.append("migration stamps differ after restore")
    if dst_snap["table_count"] != src_snap["table_count"]:
        failures.append(
            f"table count differs: source={src_snap['table_count']} restored={dst_snap['table_count']}")
    iso_errors = check_tenant_isolation(dst)
    failures.extend(iso_errors)
    # Release variant: 033/034 must be ABSENT on the restored DB —
    # the ledger columns and claim index are excluded by design.
    cols = {r["column_name"] for r in dst.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'harness_costs'").fetchall()}
    if {"model", "correlation_id"} & cols:
        failures.append("harness_costs unexpectedly has model/correlation_id (033 present)")
    idx = dst.execute(
        "SELECT 1 FROM pg_indexes WHERE tablename = 'harness_jobs' "
        "AND indexname = 'idx_harness_jobs_status_due'").fetchone()
    if idx is not None:
        failures.append("idx_harness_jobs_status_due unexpectedly present (034 present)")

    # --- migration runner must be a no-op on the restored db --------------
    print("[8/8] re-running migration runner against restored DB (must be a no-op)")
    env2 = dict(os.environ)
    env2["APP_ENV"] = "development"
    env2["DATABASE_URL"] = db_url(RESTORE_DB)
    env2.pop("DATABASE_PATH", None)
    result = run([sys.executable, "-m", "migrations.runner"], env=env2, check=False)
    if result.returncode != 0:
        failures.append("migration runner failed against restored DB")
    elif "pending=none" not in (result.stdout + result.stderr):
        failures.append("migration runner on restored DB reported pending migrations")
    dst.close()

    elapsed = time.time() - t0
    print()
    print("== DRILL RESULT ==")
    print(f"   dump file:     {dump_file}")
    print(f"   dump size:     {dump_size} bytes (sha256 {dump_sha[:16]}...)")
    print(f"   dump time:     {dump_seconds:.1f}s   restore time: {restore_seconds:.1f}s   total: {elapsed:.1f}s")
    print(f"   tables:        {src_snap['table_count']} (source) / {dst_snap['table_count']} (restored)")
    print(f"   migrations:    {len(src_snap['stamps'])}/34 stamps (source) / {len(dst_snap['stamps'])}/34 (restored)")
    print(f"   rows tracked:  {total_rows} (source) / {sum(dst_snap['counts'].values())} (restored)")
    print("   per-table counts (source -> restored):")
    for table in HASH_TABLES:
        print(f"     {table}: {src_snap['counts'][table]} -> {dst_snap['counts'][table]}")
    if failures:
        print(f"   RESULT: FAIL ({len(failures)} problem(s))")
        for failure in failures:
            print(f"     - {failure}")
    else:
        print("   RESULT: PASS — restore is row-for-row identical, stamps match, tenant isolation intact")
    if not args.keep:
        run(["dropdb", "--if-exists", SOURCE_DB], check=False)
        run(["dropdb", "--if-exists", RESTORE_DB], check=False)
        print("   drill databases dropped (--keep to retain them)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
