# Database safety (production)

## Root cause of paid-user data loss

Production previously used a **SQLite file** (`DATABASE_PATH=real_estate.db`) on the Railway **container filesystem**. That filesystem is ephemeral: redeploys and new containers discard the file, so users, tasks, leads, and SMS history disappeared.

## Required production architecture

| Item | Value |
|------|--------|
| Engine | **PostgreSQL** (Railway managed plugin) |
| Connection | `DATABASE_URL` (injected by Railway when Postgres is linked) |
| App env variable | `APP_ENV=production` (`ENV` is a fallback alias only) |
| Local-only path | `DATABASE_PATH` — **ignored** when `DATABASE_URL` is set; never the production store |
| Owner | Railway Postgres service linked to the `web` service |
| Not allowed | SQLite, local hosts, `/tmp`, test DB URLs, destructive reset flags |

### Environment variables the app reads

| Variable | Role |
|----------|------|
| `APP_ENV` | Primary environment: `production` \| `staging` \| `development` \| `test` |
| `ENV` | Used only if `APP_ENV` is unset |
| `DATABASE_URL` | PostgreSQL connection string; wins over `DATABASE_PATH` |
| `DATABASE_PATH` | SQLite file for local development/test only |
| `ALLOW_DESTRUCTIVE_DB_RESET` | Must be false in production/staging |
| `ALLOW_SQLITE_TABLE_REBUILD` | Must be false in production/staging |
| `RUN_DEMO_SEED_ON_STARTUP` | Must be false in production/staging |

---

## Production cutover sequence (SQLite → Railway Postgres)

This cutover **discards** ephemeral SQLite tester data. There is no automated SQLite→Postgres data migration.

```text
Create and link Railway PostgreSQL
→ expose DATABASE_URL to the web service
→ set APP_ENV=production
→ remove or ignore DATABASE_PATH in production Variables
→ deploy (forward-only migrations create empty schema)
→ create fresh tester / paid-access data
→ restart service → verify data still present
→ second deploy → verify data still present
```

### Railway setup (one-time)

1. In the Railway project, **New → Database → PostgreSQL**.
2. Open the **web** service → **Variables**.
3. Ensure `DATABASE_URL` is present (Railway auto-injects it when Postgres is linked to `web`).
4. Set `APP_ENV=production` (optional: `ENV=production`; `APP_ENV` wins).
5. Confirm these are **unset or false**:
   - `ALLOW_DESTRUCTIVE_DB_RESET`
   - `ALLOW_SQLITE_TABLE_REBUILD`
   - `RUN_DEMO_SEED_ON_STARTUP`
6. **Remove `DATABASE_PATH`** from production Variables (recommended). If left set, the app still ignores it whenever `DATABASE_URL` is present.
7. Redeploy `web`. Startup:
   - refuses unsafe DB config
   - applies pending migrations only (`python -m migrations.runner`)
   - starts gunicorn
8. Create fresh tester account and paid-access data in Postgres.
9. **Verify survival:**
   - Restart the `web` service → login and confirm tasks/leads still exist.
   - Trigger a second deploy → confirm the same rows still exist.
10. Confirm startup logs show `engine=postgres`, `postgres_active=true`, and migration versions — never a full `DATABASE_URL` or password.

### Post-cutover verification checklist

- [ ] `DATABASE_URL` present on `web` and points at Railway Postgres (not SQLite)
- [ ] `APP_ENV=production`
- [ ] `DATABASE_PATH` removed from production (or confirmed ignored)
- [ ] Destructive/seed flags false or unset
- [ ] Logs: `postgres_active=true` and `Migration state: ... latest=...`
- [ ] Fresh tester data created
- [ ] Data survives service restart
- [ ] Data survives a second deployment

Each environment must use a **different** database:

| APP_ENV | Database |
|---------|----------|
| production | Railway production Postgres |
| staging | Separate Railway staging Postgres |
| development | Local SQLite (`DATABASE_PATH`) or local Postgres |
| test | Temp SQLite file used by pytest |

## What startup is allowed to do

- Apply **pending** additive migrations recorded in `schema_migrations`
- Insert **idempotent** default voice personas if none exist
- Refuse to boot if production DB config is unsafe
- Log safe summary: `app_env`, `engine`, `postgres_active`, migration versions (never secrets)

## What startup must never do

- Point production at SQLite / `DATABASE_PATH`
- `DROP TABLE` / `DROP DATABASE` / `TRUNCATE`
- Rebuild schema from scratch
- Auto-run demo/seed user data
- Enable destructive reset flags
- Copy or migrate legacy SQLite rows automatically

## Safe migration command

```bash
python -m migrations.runner
```

- Location: `migrations/versions/`
- Order: `001_baseline` → same-txn verify → stamp → **commit** → post-commit fresh-connection verify → `002` → `003`
- Rules: forward-only, additive, versioned, reviewed, non-destructive
- **Transaction owner:** `migrations/runner.py` only. Migration modules must not set `autocommit`, BEGIN, COMMIT, or ROLLBACK.
- Postgres DDL uses raw psycopg execute helpers (`migrations/pg_ddl.py`) inside the runner transaction (CREATE TABLE/INDEX are transactional)
- `001` is stamped only after required tables exist in the current transaction; the whole unit commits atomically (or rolls back with no ledger row)
- If `001_baseline` was falsely recorded but base tables are missing **and the DB has no app data**, startup clears **only** `schema_migrations` rows and re-applies baseline — never on a non-empty database, never `DROP` user tables
- New columns: additive migrations use `ADD COLUMN IF NOT EXISTS` / existence checks

## Prohibited production reset commands

Do **not** run against production:

```bash
# FORBIDDEN — examples of destructive operations
DROP TABLE ...;
DROP DATABASE ...;
TRUNCATE ...;
# FORBIDDEN — app flags
ALLOW_DESTRUCTIVE_DB_RESET=true
ALLOW_SQLITE_TABLE_REBUILD=true
RUN_DEMO_SEED_ON_STARTUP=true
# FORBIDDEN — production SQLite
DATABASE_PATH=real_estate.db   # with DATABASE_URL unset / APP_ENV not forcing Postgres
```

## Pre-deployment backup (Railway Postgres)

Before each production deploy:

```bash
# From a machine with network access to the DB (use Railway's DATABASE_URL or public URL)
pg_dump "$DATABASE_URL" --format=custom --file="backup-$(date +%Y%m%d-%H%M%S).dump"
```

Or use Railway’s dashboard: Postgres service → **Backups** / snapshot if enabled on your plan.

Store the dump outside the app container (local secure storage or object storage).

## Restore steps

```bash
# WARNING: restores replace target database contents. Use only for disaster recovery.
pg_restore --clean --if-exists --no-owner --dbname="$DATABASE_URL" backup-YYYYMMDD-HHMMSS.dump
```

After restore:

1. Confirm `APP_ENV=production` and `DATABASE_URL` still point at the production Postgres.
2. Run `python -m migrations.runner` (applies only missing migrations).
3. Restart the web service.
4. Spot-check: login as a known subscriber, verify Tasks/Leads row counts.

## Task tenancy

Tasks are keyed by:

- `tasks.id` (stable database ID)
- `tasks.user_id` (paid account owner)
- `tasks.assigned_user_id`
- optional `tasks.lead_id` (same owner)

APIs always filter by `user_id`. There is no SQL `ON DELETE CASCADE` from redeploy/reseed/reauth. Permanent records are never keyed only by browser sessions, temporary auth tokens, deployment IDs, or local files.
