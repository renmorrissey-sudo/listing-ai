#!/usr/bin/env bash
# backup_postgres.sh — logical backup of the TopAI Postgres database.
#
# Usage:
#   DATABASE_URL='postgresql://...' scripts/backup_postgres.sh [--dry-run] [--out-dir DIR] [--confirm-production] [--retain-days N]
#
# What it does:
#   pg_dump --format=custom --no-owner "$DATABASE_URL" to a timestamped
#   .dump file, verifies the dump with `pg_restore --list`, and prints
#   the file path, size, and SHA-256.
#
# Safety:
#   - Read-only against the database (pg_dump never writes to it).
#   - Refuses SQLite URLs and an empty DATABASE_URL.
#   - FAIL-SAFE: refuses a production-looking DATABASE_URL unless you
#     pass --confirm-production (or set BACKUP_CONFIRM_PRODUCTION=1).
#     "Production-looking" = the host is not localhost/127.0.0.1/::1,
#     OR the database name is `railway` (Railway's default production
#     database name — this also catches a `railway connect postgres
#     --tunnel-only` tunnel, which presents as localhost). A local
#     scratch/dev database never needs the flag.
#   - NEVER prints DATABASE_URL or any credential. --dry-run prints the
#     command with the URL replaced by <DATABASE_URL>.
#   - Retention: if BACKUP_RETAIN_DAYS (or --retain-days N) is set, dump
#     files older than N days are pruned afterwards — but the newest
#     dump is NEVER pruned. No pruning happens by default.
#   - Does NOT restore anything and does NOT touch any service.
#
# Production use (from a machine with the Railway DATABASE_URL or
# DATABASE_PUBLIC_URL — copy it from the Postgres service Variables tab;
# prefer a `railway connect postgres --tunnel-only` tunnel for large DBs):
#   DATABASE_URL='<redacted>' scripts/backup_postgres.sh --confirm-production
# Take one before EVERY production deploy (see docs/backup-and-restore.md).
# This script itself is never wired to production and never runs on a
# schedule by itself — run it manually, or from the cron-service pattern
# in docs/backup-and-restore.md §4 once Ren approves it.

set -eu
DRY_RUN=0
CONFIRM_PRODUCTION=0
OUT_DIR="${BACKUP_OUT_DIR:-./backups}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --confirm-production) CONFIRM_PRODUCTION=1; shift ;;
        --out-dir)
            if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
                echo "ERROR: --out-dir needs a directory argument." >&2; exit 2
            fi
            OUT_DIR="$2"; shift 2 ;;
        --out-dir=*) OUT_DIR="${1#--out-dir=}"; shift ;;
        --retain-days)
            if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
                echo "ERROR: --retain-days needs a number of days." >&2; exit 2
            fi
            RETAIN_DAYS="$2"; shift 2 ;;
        --retain-days=*) RETAIN_DAYS="${1#--retain-days=}"; shift ;;
        -h|--help)
            sed -n '2,36p' "$0"
            exit 0
            ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -n "${BACKUP_CONFIRM_PRODUCTION:-}" ] && [ "${BACKUP_CONFIRM_PRODUCTION}" = "1" ]; then
    CONFIRM_PRODUCTION=1
fi

case "${RETAIN_DAYS}" in
    "") ;;
    *[!0-9]*)
        echo "ERROR: --retain-days / BACKUP_RETAIN_DAYS must be a whole number of days, got '${RETAIN_DAYS}'." >&2; exit 2 ;;
esac

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL is not set. Export it first (never paste it into chat or a file)." >&2
    exit 2
fi
case "$DATABASE_URL" in
    sqlite:*|*".db"*|*"real_estate.db"*)
        echo "ERROR: DATABASE_URL looks like SQLite — refusing. This script is for Postgres only." >&2
        exit 2
        ;;
    postgres://*|postgresql://*)
        ;;
    *)
        echo "ERROR: DATABASE_URL does not look like a Postgres URL (expected postgres:// or postgresql://)." >&2
        exit 2
        ;;
esac

# --- production fail-safe -------------------------------------------------
# Parse host and database name out of the URL without ever printing it.
#   postgresql://user:pass@host:port/dbname?params
_url_rest="${DATABASE_URL#*://}"
case "$_url_rest" in
    *@*) _url_hostport="${_url_rest#*@}" ;;
    *)   _url_hostport="$_url_rest" ;;
esac
# Bracketed IPv6 literal, e.g. [::1]:5432/db
case "$_url_hostport" in
    \[*\]*)
        _url_host="${_url_hostport%%\]*}"
        _url_host="${_url_host#\[}"
        _url_after="${_url_hostport#*\]}"
        case "$_url_after" in
            /*) _url_db="${_url_after#/}" ;;
            *)  _url_db="" ;;
        esac
        ;;
    *)
        _url_host="${_url_hostport%%[:/]*}"
        case "$_url_hostport" in
            */*) _url_db="${_url_hostport#*/}" ;;
            *)   _url_db="" ;;
        esac
        ;;
esac
_url_db="${_url_db%%\?*}"

_is_production=0
case "$_url_host" in
    localhost|127.0.0.1|::1) ;;
    *) _is_production=1 ;;
esac
# Railway's default production database is named `railway`, including
# over a localhost tunnel — a localhost host alone is not proof of local.
if [ "$_url_db" = "railway" ]; then
    _is_production=1
fi

if [ "$_is_production" -eq 1 ] && [ "$CONFIRM_PRODUCTION" -ne 1 ]; then
    echo "ERROR: DATABASE_URL looks like PRODUCTION (host '${_url_host}', database '${_url_db:-<none>}') — refusing without explicit confirmation." >&2
    echo "  This is the fail-safe: re-run with --confirm-production (or BACKUP_CONFIRM_PRODUCTION=1)" >&2
    echo "  only if you really mean to back up the production database." >&2
    echo "  (Local databases on localhost with a non-'railway' database name never need the flag.)" >&2
    exit 2
fi

STAMP=$(date -u +%Y%m%d-%H%M%S)
FILE="${OUT_DIR}/backup-${STAMP}.dump"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY RUN — no backup taken. Would run:"
    echo "  pg_dump \"<DATABASE_URL>\" --format=custom --no-owner --file=\"${FILE}\""
    echo "  pg_restore --list \"${FILE}\"   # verify the dump lists tables"
    echo "  sha256sum \"${FILE}\""
    exit 0
fi

# Real run only: pg_dump must exist (dry-run above does not need it).
command -v pg_dump >/dev/null 2>&1 || { echo "ERROR: pg_dump not found. Install postgresql-client (Debian/Ubuntu: apt-get install postgresql-client)." >&2; exit 2; }

mkdir -p "$OUT_DIR"
echo "Dumping database to ${FILE} ..."
pg_dump "$DATABASE_URL" --format=custom --no-owner --file="$FILE"

TABLES=$(pg_restore --list "$FILE" | grep -c " TABLE " || true)
SIZE=$(stat -c%s "$FILE" 2>/dev/null || stat -f%z "$FILE")
SHA=$(sha256sum "$FILE" | awk '{print $1}')

echo "Backup complete."
echo "  file:   ${FILE}"
echo "  size:   ${SIZE} bytes"
echo "  tables: ${TABLES} (from pg_restore --list)"
echo "  sha256: ${SHA}"
echo "Store this file outside the app container (local secure storage or object storage)."

# --- retention pruning (opt-in only) --------------------------------------
# Delete backup-*.dump files older than RETAIN_DAYS, but NEVER the newest
# dump — the most recent backup is always kept, whatever its age.
if [ -n "$RETAIN_DAYS" ]; then
    _newest=$(ls -t "${OUT_DIR}"/backup-*.dump 2>/dev/null | head -n 1 || true)
    _pruned=0
    while IFS= read -r _old; do
        [ -n "$_old" ] || continue
        if [ "$_old" != "$_newest" ]; then
            rm -f -- "$_old"
            _pruned=$((_pruned + 1))
        fi
    done <<EOF
$(find "$OUT_DIR" -maxdepth 1 -name 'backup-*.dump' -type f -mtime "+${RETAIN_DAYS}" 2>/dev/null)
EOF
    echo "Retention: pruned ${_pruned} dump(s) older than ${RETAIN_DAYS} day(s); newest dump kept."
fi
