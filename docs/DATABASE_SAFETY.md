# Database safety (production)

## Root cause of paid-user data loss

Production previously used a **SQLite file** (`DATABASE_PATH=real_estate.db`) on the Railway **container filesystem**. That filesystem is ephemeral: redeploys and new containers discard the file, so users, tasks, leads, and SMS history disappeared.

## Required production architecture

| Item | Value |
|------|--------|
| Engine | **PostgreSQL** (Railway managed plugin) |
| Connection | `DATABASE_URL` (injected by Railway when Postgres is linked) |
| Owner | Railway Postgres service linked to the `web` service |
| App env | `APP_ENV=production` |
| Not allowed | SQLite, local hosts, `/tmp`, test DB URLs, destructive reset flags |

### Railway setup (one-time)

1. In the Railway project, **New → Database → PostgreSQL**.
2. Open the **web** service → **Variables**.
3. Ensure `DATABASE_URL` is present (Railway usually auto-injects it when you link the Postgres service).
4. Set:
   - `APP_ENV=production`
   - `ENV=production` (optional alias; `APP_ENV` wins)
5. Confirm these are **unset or false**:
   - `ALLOW_DESTRUCTIVE_DB_RESET`
   - `ALLOW_SQLITE_TABLE_REBUILD`
   - `RUN_DEMO_SEED_ON_STARTUP`
6. Remove reliance on `DATABASE_PATH` in production (ignore it when `DATABASE_URL` is set).
7. Redeploy. Startup runs **forward-only migrations** only (`python -m migrations.runner`), then gunicorn.

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

## What startup must never do

- Point production at SQLite
- `DROP TABLE` / `DROP DATABASE` / `TRUNCATE`
- Rebuild schema from scratch
- Auto-run demo/seed user data
- Enable destructive reset flags

## Migrations

- Location: `migrations/versions/`
- Runner: `python -m migrations.runner`
- Rules: forward-only, additive, versioned, reviewed, non-destructive
- New columns: add a new versioned migration with `ALTER TABLE ... ADD COLUMN` only when missing

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

## Optional: copy leftover SQLite into Postgres (if you still have a file)

Only if you recovered `real_estate.db` from a volume or local backup:

```bash
# Example approach — run manually, never in app startup
# 1. Keep a copy of real_estate.db
# 2. Use a one-off ETL/script to INSERT into Postgres by stable user.id / task.id
# 3. Verify row counts before switching traffic
```

Do **not** automate this in production boot.

## Task tenancy

Tasks are keyed by:

- `tasks.id` (stable database ID)
- `tasks.user_id` (paid account owner)
- `tasks.assigned_user_id`
- optional `tasks.lead_id` (same owner)

APIs always filter by `user_id`. There is no cascade-delete from redeploy/reseed/reauth.
