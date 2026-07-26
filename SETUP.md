# TopAI Real Estate Tools — Setup Guide

## What you need
- Python 3.10+ ([download](https://www.python.org/downloads/))
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com/))

---

## Local development (SQLite)

1. Copy `.env.example` to `.env` and set at least:
   - `ANTHROPIC_API_KEY`
   - `FLASK_SECRET_KEY`
   - `APP_ENV=development`
2. Leave `DATABASE_URL` unset for local SQLite.
3. Optional: set `DATABASE_PATH=real_estate.db` (default). This is **development-only**.
4. Install and run:

```
pip install -r requirements.txt
python -m migrations.runner
python app.py
```

Open **http://localhost:8080** (or the `PORT` in `.env`).

When `DATABASE_URL` is set, the app uses PostgreSQL and **ignores** `DATABASE_PATH`.

---

## Production (Railway PostgreSQL) — required

Production paid-user data must live in **Railway PostgreSQL** via `DATABASE_URL`.  
Do **not** use `DATABASE_PATH` / SQLite on Railway (ephemeral container filesystem).

Full cutover checklist, forbidden commands, and backup/restore: **[docs/DATABASE_SAFETY.md](docs/DATABASE_SAFETY.md)**.

### Minimum production variables on the `web` service

| Variable | Required value |
|----------|----------------|
| `APP_ENV` | `production` |
| `DATABASE_URL` | Injected by linked Railway Postgres (postgresql://…) |
| `ANTHROPIC_API_KEY` | Set |
| `FLASK_SECRET_KEY` | Set |
| Stripe keys | Set in production |

Unset or `false` in production:

- `ALLOW_DESTRUCTIVE_DB_RESET`
- `ALLOW_SQLITE_TABLE_REBUILD`
- `RUN_DEMO_SEED_ON_STARTUP`

Remove `DATABASE_PATH` from production Variables (or leave it; it is ignored when `DATABASE_URL` is set).

Deploy command (Procfile): migrations then gunicorn:

```
python -m migrations.runner && gunicorn app:app --bind 0.0.0.0:$PORT ...
```

---

## Safe migration command

```
python -m migrations.runner
```

Applies only pending forward-only migrations. Never drops or truncates tables.

## Prohibited in production

- Pointing the app at SQLite (`DATABASE_PATH` without Postgres)
- `DROP TABLE` / `DROP DATABASE` / `TRUNCATE`
- Enabling destructive reset or demo-seed flags
- Recreating the database on every deploy

---

## Tests

```
pytest
```

Uses a temporary SQLite file (`APP_ENV=test`). Each environment (production, staging, development, test) must use a **separate** database.
