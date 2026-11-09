# Database Backup & Restore — TopAI Real Estate Tools

**Scope:** the production Railway Postgres database for TopAI (project
`gracious-expression`). Companion docs: `docs/DATABASE_SAFETY.md`
(production architecture, pre-deploy backup command, restore command),
`docs/harness-railway-checklist.md` (the Railway-dashboard checklist,
§7 completion log), `docs/harness-staging.md` §6 (open items H5/H6).

**Status of this document:** written 2026-11-08 on branch
`topai-harness-hardening-backup`. Railway's backup capabilities (§1)
are taken from Railway's public documentation (sources cited inline) —
**they have NOT been confirmed against the live `gracious-expression`
project**, because no Railway access was available to this work. The
local backup/restore drill (§6) WAS executed against isolated local
Postgres 16 and its results are recorded there. The production items
that only Ren (or someone with Railway access) can confirm are listed
in §7 — do not mark them done until observed in the dashboard.

---

## 1. What Railway provides for Postgres (from Railway's public docs)

Railway Postgres stores data on a volume. Railway documents three
backup layers; its own guide recommends using **all three** for
production ("Use all three for production").

Sources:
- Railway guide "Back Up and Restore Postgres" (docs, dated
  2026-07-29): `https://github.com/railwayapp/docs/blob/HEAD/content/guides/postgres-backups-restores.md`
- Railway CLI reference, `railway postgres`: `https://docs.railway.com/cli/postgres`
- Railway template "PostgreSQL 18 with Point-in-Time Recovery":
  `https://railway.com/deploy/postgresql-18-with-point-in-time-recover`

### Layer 1 — Volume backups (scheduled or manual snapshots)

- Snapshots of the volume attached to the Postgres service.
  Incremental and copy-on-write; billed only for data unique to each
  snapshot, at the same per-GB rate as volumes.
- Enable in the Postgres service → **Backups** tab. Schedules, each
  with its own retention:
  - **Daily:** every 24 hours, kept for **6 days**
  - **Weekly:** every 7 days, kept for **1 month**
  - **Monthly:** every 30 days, kept for **3 months**
- A manual backup can be triggered from the same tab at any time.
  Via CLI: `railway postgres pitr backup create [--name <NAME>]`,
  `backup list`, `backup delete`, `backup lock` (removes a backup's
  expiration), `backup restore <ID>`.
- Restore: Backups tab → find the backup by date stamp → **Restore**.
  Railway stages the change (a new volume named after the backup's
  date stamp is mounted; the previous volume is unmounted but
  retained); review the staged change and click **Deploy** to apply.
  The service redeploys on the restored data.
- **Limits (from Railway's docs):**
  - Manual backups are limited to **50% of the volume's total size**.
    If data is above that threshold, grow the volume first.
  - **Wiping a volume deletes all of its backups.** Volume backups
    protect against data mistakes, not against deleting the volume
    itself.
  - Restoring a backup **removes any newer backups created after
    it**, and backups can only be restored into the **same project
    and environment**.

### Layer 2 — Point-in-time recovery (PITR, continuous WAL archiving)

- When enabled, the Postgres image archives every WAL segment to a
  private Railway storage bucket using **pgBackRest**, and takes
  rolling base backups (**weekly full, daily incremental/differential**).
  The **last 4 full backups are retained, giving a restore window of
  roughly 4 weeks** (the template page says about 3–4 weeks).
- Enable: Postgres service → **Backups** tab → **Enable PITR** and
  confirm. Railway creates a bucket named **Postgres-PITR**, sets
  `WAL_ARCHIVE_*` variables on the service, and redeploys it. Once
  archiving is healthy and the first base backup completes, a datetime
  picker appears on the Backups tab.
- Restore: pick a timestamp → **Restore to this moment**. Railway
  creates a **new Postgres service** next to the original, named
  `<source>-restored-YYYYMMDD-HHMM`, and replays base backup + WAL
  forward to the target. **The source service is untouched** and keeps
  serving traffic. Verify the fork's data, then cut over by swapping
  connection strings or copying rows back. The fork runs as plain
  non-archiving Postgres; enable PITR on it separately for continued
  coverage.
- Via CLI: `railway postgres pitr status | enable | disable |
  restore --at <TIME> [--new-service-name <NAME>]`. The restore
  target accepts RFC 3339, local `YYYY-MM-DD HH:MM[:SS]`, or a
  relative duration such as `30m`, `2h`, `1d`, `1w`.
- **Limits:**
  - The restore window **starts from the first post-enable base
    backup**. Enabling PITR today does **not** let you restore to
    yesterday — enable it before you need it.
  - PITR requires an official Railway Postgres image
    (`ghcr.io/railwayapp-templates/postgres-ssl` or the HA
    `postgres-patroni` image), a **major version tag** such as `:16`
    (minor tags such as `:16.10` are not supported).
  - On HA clusters, enabling rolls through the cluster with about
    **5 seconds of unavailability** during the switchover.
  - PITR has **no separate fee**; you pay bucket storage at the
    standard rate for the compressed WAL and base backups, plus
    network egress for the uploads (idle databases archive almost
    nothing).

### Layer 3 — Logical dumps (`pg_dump`, the layer you control)

- A portable SQL-level export you create yourself. Restores
  anywhere: another Railway service, another provider, your laptop.
  **The only layer that survives deleting the project itself.**
- Needs the Postgres client tools locally (`pg_dump`, `pg_restore`,
  `psql`). Debian/Ubuntu: `apt-get install postgresql-client`.
  macOS: `brew install libpq`.
- Reach the database from your machine either through a Railway CLI
  tunnel (`railway connect postgres --tunnel-only`, in a project
  directory linked with `railway link` — it prints host/port/user/
  password/database without launching a client), or through the
  public TCP proxy using the `DATABASE_PUBLIC_URL` variable from the
  Postgres service. Traffic over the TCP proxy is billed as network
  egress, so the tunnel is preferable for large databases dumped
  often.
- Production command (placeholders only — never a real URL):

  ```bash
  pg_dump "postgresql://postgres:<PASSWORD>@localhost:<PORT>/railway" \
    --format=custom \
    --no-owner \
    --file=backup-$(date +%Y%m%d-%H%M%S).dump
  ```

  or, with the public URL:

  ```bash
  pg_dump "$DATABASE_PUBLIC_URL" --format=custom --no-owner --file=backup.dump
  ```

  Custom format is compressed and lets `pg_restore` do selective
  and parallel restores, which plain SQL dumps cannot. The repo's
  wrapper script `scripts/backup_postgres.sh` runs exactly this
  (plus a `pg_restore --list` verification and a SHA-256) — see §3.

### What Railway does NOT provide (per its own docs)

- **No automatic backups by default.** Volume backups and PITR are
  features you **enable once**; nothing in Railway's docs says a new
  Postgres service ships with backups on. Until someone enables
  them in the dashboard, the production database has **no Railway
  backup at all** — `pg_dump` is then the only backup.
- **No offsite copy.** Volume backups and PITR archives live inside
  the same Railway project/account/region as the database. They do
  not protect against losing the project or the Railway account.
  Only a logical dump stored outside Railway does that.
- **No backup of a deleted volume.** Wiping the volume deletes its
  backups (Layer 1). PITR's bucket is separate, but it is still a
  Railway-managed bucket in the same project.
- **No retroactive PITR window.** The window starts at enablement.
- **No guaranteed retention beyond the stated windows** (daily 6
  days, weekly 1 month, monthly 3 months, PITR ~4 weeks). Anything
  older than that exists only if you kept a logical dump.
- **No restore drill.** Railway does not verify your dumps restore.
  That is the operator's job (§6).

---

## 2. Backup policy for this project (what we commit to)

Production currently has **zero paying customers** and a small
database, so the policy is deliberately simple and cheap. Revisit it
when the first paying customer lands or the database grows past
~1 GB.

| Layer | Schedule | Retention | Owner |
|---|---|---|---|
| Railway volume backups | Daily + Weekly enabled in dashboard | Daily 6 days, Weekly 1 month (Railway-fixed) | Ren / Railway dashboard |
| Railway PITR | Enabled once, then continuous | ~4 weeks (Railway-fixed, last 4 fulls) | Ren / Railway dashboard |
| Logical dump (`pg_dump`) | **Before every production deploy** (mandatory), plus **weekly** (e.g. Sunday 03:00 UTC) | **90 days** of weekly dumps + **every pre-deploy dump for 30 days**; keep the most recent pre-deploy dump indefinitely until superseded by a verified newer one | Whoever deploys (manual until the cron service in §4 is approved) |

**Pre-deploy backup is a hard gate, not a suggestion.** No
production deploy goes out without a fresh `pg_dump` taken first and
recorded in the completion log (`docs/harness-railway-checklist.md`
§7) with date, file name, size, and SHA-256. The Nov 8 production
deploy had no pre-deploy backup — that gap is recorded, not
retroactively fixable; the first evidenced backup after this document
is the new baseline.

**Dump storage:** outside the app container and outside the Railway
project — local secure storage (encrypted disk) or an S3-compatible
bucket the team controls. Never commit a dump to the repo (enforced by
`.gitignore`: `backups/` and `*.dump`). Never paste a `DATABASE_URL`
into chat, a ticket, or a log.

**Retention pruning (script support):** `scripts/backup_postgres.sh`
prunes dump files older than `BACKUP_RETAIN_DAYS` (or
`--retain-days N`) when that is set — but it **never** prunes the
newest dump, whatever its age. No pruning happens by default, so a
forgotten flag cannot delete the only copy. Set
`BACKUP_RETAIN_DAYS=90` for weekly dumps and `30` for the pre-deploy
directory to implement the table above.

**What counts as "the backup works":** a dump is only a backup once
a restore drill has proven it restores (§6). A folder of
never-restored dumps is not a backup strategy.

---

## 3. Taking a backup (manual, today)

Prerequisites: PostgreSQL client tools installed locally
(`pg_dump --version` works).

```bash
# 1. Copy DATABASE_URL (or DATABASE_PUBLIC_URL) from the production
#    Postgres service → Variables tab. Treat it as a password: paste
#    it only into the shell command below, never into chat/files.
#    Prefer a tunnel for large DBs (no egress charge on the read):
#      railway link   # once, in the project directory
#      railway connect postgres --tunnel-only   # leave running; note host/port/password
#    ...then use the tunnel URL in place of $DATABASE_URL below.

# 2. Run the wrapper (dry-run first to see the exact command).
#    The fail-safe refuses a production-looking DATABASE_URL unless
#    --confirm-production is given (non-localhost host, or database
#    named `railway` — including over a localhost tunnel). Local
#    scratch/dev databases never need the flag.
DATABASE_URL='<redacted>' scripts/backup_postgres.sh --dry-run --confirm-production
DATABASE_URL='<redacted>' scripts/backup_postgres.sh --confirm-production

# The script writes ./backups/backup-YYYYMMDD-HHMMSS.dump, verifies it
# with pg_restore --list, and prints size + SHA-256. Record all three
# in docs/harness-railway-checklist.md §7.
```

Equivalent raw command (what the script runs):

```bash
pg_dump "$DATABASE_URL" --format=custom --no-owner --file="backup-$(date +%Y%m%d-%H%M%S).dump"
pg_restore --list "backup-$(date +%Y%m%d-%H%M%S).dump" | head   # must list app tables
sha256sum backup-*.dump
```

---

## 4. Automating offsite dumps (proposed — needs Ren's approval)

Railway's documented pattern: a **cron service** in the project that
runs `pg_dump` on a schedule and uploads the result to a Railway
storage bucket (or any S3-compatible store outside Railway).

- Image: `postgres:16-alpine` + `aws-cli`; the service runs a
  `backup.sh` that dumps `$DATABASE_URL` (a reference to the Postgres
  service's `DATABASE_URL`, so the dump travels over private
  networking with no egress cost for the read) and uploads to the
  bucket with `aws s3 cp ... --endpoint-url "$ENDPOINT"`.
- Schedule: `0 3 * * *` (daily 03:00 UTC). Railway cron runs in UTC,
  minimum granularity 5 minutes; if a run is still going when the
  next trigger fires, the new run is skipped rather than stacked, so
  the service must exit when the dump finishes.
- Bucket storage is billed at $0.015/GB-month with free API
  operations; add a cleanup step (`aws s3 rm` on objects older than
  the §2 retention window) once the bucket accumulates.
- **Not built yet.** Creating the bucket + cron service is a Railway
  dashboard step and a small new service definition; it needs Ren's
  go-ahead (it creates recurring spend, however small, and a new
  service holding a database credential). Until it exists, §3 manual
  dumps are the backup. Do not present the cron service as done.

---

## 5. Restore procedures

### 5a. Restore a volume backup (dashboard)

1. Production Postgres service → **Backups** tab → find the backup
   by date stamp → **Restore**.
2. Railway stages the change (new volume mounted, old volume
   retained). Review the staged change → **Deploy**.
3. The service redeploys on the restored data.
4. Verify: §5d checks below.
5. Remember: restoring removes any newer backups created after the
   restored one, and only works in the same project/environment.

### 5b. Restore to a point in time (dashboard or CLI)

```bash
railway postgres pitr restore --at "2026-11-08 14:30" --new-service-name postgres-restored-20261108-1430
# --at also accepts RFC 3339 or a relative duration: 30m, 2h, 1d, 1w
```

1. Railway creates a **new sibling Postgres service**; the source
   is untouched and keeps serving traffic.
2. Verify the fork's data (§5d) **before** cutting over.
3. Cut over by swapping the `DATABASE_URL` reference on `web` /
   `worker` to the fork (or copy the rows you need back into the
   source). The fork runs as plain non-archiving Postgres — enable
   PITR on it separately if you want continued coverage.
4. Delete the fork only after the cutover is verified and a fresh
   backup of the live database exists.

### 5c. Restore a logical dump (pg_restore — the disaster-recovery path)

Use this when the volume/PITR layers are unavailable, or when you
need to restore **offsite** (new provider, new project, local
verification). **Never restore into the production database
directly.** Always restore into a scratch database first, verify,
then cut over.

```bash
# 1. Create a scratch target (a second Railway Postgres in the
#    project, or a local Postgres database). Get its URL.
#    NEVER use the production DATABASE_URL as the restore target.

# 2. Restore the dump into the scratch target:
pg_restore --clean --if-exists --no-owner \
  --dbname="<scratch DATABASE_URL>" backup-YYYYMMDD-HHMMSS.dump

# 3. Verify (§5d). Only then decide how to cut over:
#    - Preferred: point staging/web at the scratch DB after
#      verification, or copy the needed rows back into production
#      with explicit, reviewed SQL.
#    - Direct in-place restore into production is a last resort and
#      needs Ren's explicit go-ahead in the moment: it REPLACES the
#      production database contents.
```

**Automated version (local drill):**
`scripts/backup_restore_drill.py` runs this whole loop — migrate,
seed, dump, restore into a fresh DB, compare row-for-row — against
isolated local Postgres. It refuses any non-localhost host and any
database name not ending in `_drill`. See §6.

### 5d. Verification after ANY restore (all three layers)

Run these before calling a restore done:

1. **Migration stamps:** `SELECT version FROM schema_migrations
   ORDER BY version;` — must list 001..032 (32 stamps), latest
   `032_tenant_sms_senders_scoped_unique`. Then run
   `python -m migrations.runner` against the restored DB — it must
   report `pending=none` (no-op).
2. **Table inventory:** count of tables in `information_schema`
   (49 in the Nov 8 staging validation) matches the pre-incident
   count.
3. **Row counts:** compare `SELECT count(*) FROM users;`,
   `harness_settings`, `harness_jobs`, `harness_audit`,
   `harness_costs`, `harness_approvals`, `tenant_sms_senders`,
   `leads`, `tasks`, `sms_messages` against the counts recorded at
   dump time (or against production, for a PITR/volume restore).
4. **Tenant isolation spot checks:**
   - The shared platform number `+18888210810` may appear once per
     tenant in `tenant_sms_senders`; a duplicate
     `(user_id, sender_number)` insert must fail.
   - The same `idempotency_key` may appear once per tenant in
     `harness_jobs`; no two jobs for the same tenant share a key.
   - `harness_jobs.correlation_id` is non-NULL on jobs created after
     migration 031.
5. **App smoke test:** start the app against the restored DB
   (staging or local), log in, confirm the dashboard loads and a
   known tenant's leads/tasks are present.
6. **Record it:** date, restore layer used, target, `pg_restore`
   exit status, compared counts, and any warnings — in
   `docs/harness-railway-checklist.md` §7.

### 5e. If a restore fails

1. **Stop.** Do not retry the same restore into the same target
   repeatedly — repeated `--clean` restores against a half-restored
   database can mask the original error.
2. Read the full `pg_restore` output. Common, usually-benign
   warnings: "already exists" for extensions (e.g. `plpgsql`) when
   restoring into a non-empty DB — acceptable **only** if the
   restore was run without `--clean` into a DB that already had the
   extension. Errors about **missing tables, missing columns, or
   constraint violations are not acceptable** — the dump or the
   target is wrong.
3. Check the dump itself: `pg_restore --list <file>` must list the
   app tables; a near-empty or 0-byte file means the dump failed
   (wrong URL, wrong database, connection dropped) — take a fresh
   dump if the source database is still alive.
4. Try the next layer down the list: volume backup → PITR fork →
   older logical dump → previous week's dump. Each layer is
   independent; a corrupt dump does not affect the volume backups.
5. If the production database is still alive, take a **fresh**
   `pg_dump` immediately (it captures the current state, even if
   damaged — a damaged backup beats no backup) and then work from
   the volume/PITR layers for the actual recovery.
6. If **no** layer yields a verified restore, escalate to Ren with:
   the incident time, the last verified backup (date + SHA-256),
   the exact `pg_restore` error output, and which layers were tried.
   Do not improvise schema surgery on production.
7. After any incident: record what failed and why in the checklist
   §7 log, fix the cause (e.g. a dump job that was silently failing),
   and re-run the §6 drill before the next deploy.

---

## 6. Restore drill — executed 2026-11-08 (isolated local Postgres 16)

<!-- DRILL-RESULTS: filled in by the drill run; do not edit by hand
     without re-running scripts/backup_restore_drill.py. -->

**Result: PASS — 2026-11-08 (UTC 2026-11-09 02:34), isolated local
Postgres 16.15.** Command:
`.venv/bin/python scripts/backup_restore_drill.py` (exit 0, total
9.5 s). Full log: `/tmp/drill-run2.log` on the drill machine; the
dump artifact is `/tmp/topai-backup-drill/backup-20261109-023447.dump`
(kept on the drill machine only — synthetic data, not production).

| Check | Source | Restored | Match |
|---|---|---|---|
| Migration stamps | 32/32 (001..032, latest `032_tenant_sms_senders_scoped_unique`) | 32/32, identical list | ✅ |
| Tables (`information_schema`, public) | 49 | 49 | ✅ |
| Rows in tracked tables | 72 | 72 | ✅ |
| `users` / `harness_settings` | 2 / 2 | 2 / 2 | ✅ |
| `harness_jobs` / `harness_audit` | 8 / 8 | 8 / 8 | ✅ |
| `harness_costs` / `harness_memory` / `harness_approvals` | 2 / 1 / 2 | 2 / 1 / 2 | ✅ |
| `tenant_sms_senders` (incl. shared `+18888210810` × 2 tenants) | 3 | 3 | ✅ |
| `leads` / `tasks` / `sms_messages` | 4 / 4 / 4 | 4 / 4 / 4 | ✅ |
| Content hashes (SHA-256 of ordered rows, 11 key tables) | — | identical for all 11 | ✅ |
| Migration runner re-run on restored DB | — | `pending=none` (no-op) | ✅ |
| Tenant isolation after restore | shared number × 2 tenants OK; duplicate `(user_id, sender_number)` rejected; same `idempotency_key` × 2 tenants OK; 0 NULL `correlation_id` | — | ✅ |

- **Dump:** `pg_dump --format=custom --no-owner`, **150,764 bytes**,
  SHA-256
  `871b01edea5d973315c09f05014baf310d37635b0ec878900b6485aa981b0d28`,
  took **0.8 s**. `pg_restore --list`: 98 entries (49 TABLE + 49
  TABLE DATA).
- **Restore:** `pg_restore --no-owner --exit-on-error` into a fresh
  database, took **1.4 s**, exit 0, **zero errors and zero warnings**.
- **Migrations:** full set 001..032 applied to the empty source DB
  in **3.4 s**.
- **Seed data (synthetic, 2 tenants):** tenant A `drafts_only`,
  tenant B `supervised`; 8 jobs (incl. `succeeded`/`pending`/
  `failed`/`dead_letter`), 8 audit rows chained by `correlation_id`,
  2 cost rows, 2 approvals (1 pending, 1 approved), 1 memory row,
  3 sender rows, 4 leads, 4 tasks, 4 SMS messages.
- **What this proves:** the dump/restore loop works end to end on
  the real migration set and the real harness schema, including the
  030 per-tenant idempotency scoping and the 032 tenant-scoped
  sender uniqueness. **What it does NOT prove:** anything about the
  production Railway database — that needs §7 items 4–5 (a dump of
  the real production DB restored into a scratch DB).

### Re-run 2026-11-08 (UTC 2026-11-09 03:14) — fresh drill, branch `launch-backup-verify`

**Result: PASS — fresh isolated local Postgres 16.15, exit 0, total
7.8 s.** Command:
`.venv/bin/python scripts/backup_restore_drill.py --dump-file
drill/backup-20261109-031406.dump`. Full log:
`drill/drill-2026-11-08.log` in this worktree; the dump artifact is
`drill/backup-20261109-031406.dump` (kept in the worktree, synthetic
data, not production — `*.dump` is gitignored, so it is not committed;
its SHA-256 below is the record).

| Check | Source | Restored | Match |
|---|---|---|---|
| Migration stamps | 32/32 (001..032, latest `032_tenant_sms_senders_scoped_unique`) | 32/32, identical list | ✅ |
| Tables (`information_schema`, public) | 49 | 49 | ✅ |
| Rows in tracked tables | 72 | 72 | ✅ |
| `users` / `harness_settings` | 2 / 2 | 2 / 2 | ✅ |
| `harness_jobs` / `harness_audit` | 8 / 8 | 8 / 8 | ✅ |
| `harness_costs` / `harness_memory` / `harness_approvals` | 2 / 1 / 2 | 2 / 1 / 2 | ✅ |
| `tenant_sms_senders` (incl. shared `+18888210810` × 2 tenants) | 3 | 3 | ✅ |
| `leads` / `tasks` / `sms_messages` | 4 / 4 / 4 | 4 / 4 / 4 | ✅ |
| Content hashes (SHA-256 of ordered rows, 11 key tables) | — | identical for all 11 | ✅ |
| Migration runner re-run on restored DB | — | `pending=none` (no-op) | ✅ |
| Tenant isolation after restore | shared number × 2 tenants OK; duplicate `(user_id, sender_number)` rejected; same `idempotency_key` × 2 tenants OK; 0 NULL `correlation_id` | — | ✅ |

- **Dump:** `pg_dump --format=custom --no-owner`, **150,765 bytes**,
  SHA-256
  `19f87b95de54e3d2c94bd27d72b42e4bcb9a6b127e468ab328fa87188bf5b929`,
  took **0.5 s**. `pg_restore --list`: 98 entries (49 TABLE + 49
  TABLE DATA).
- **Restore:** `pg_restore --no-owner --exit-on-error` into a fresh
  database, took **0.8 s**, exit 0, **zero errors and zero warnings**
  (the only "error" string in the log is the `--exit-on-error` flag
  itself).
- **Migrations:** full set 001..032 applied to the empty source DB
  in **3.1 s**. Seed data identical in shape to the first run
  (tenant A `drafts_only`, tenant B `supervised`; 8 jobs, 8 audit
  rows, 2 cost rows, 2 approvals, 1 memory row, 3 sender rows,
  4 leads, 4 tasks, 4 SMS messages).
- **Tooling changes since the first run:** `scripts/backup_postgres.sh`
  gained the production fail-safe (`--confirm-production`, refused
  remote/`railway` URLs without it), opt-in retention pruning
  (`--retain-days` / `BACKUP_RETAIN_DAYS`, newest dump never pruned),
  and `.gitignore` now covers `backups/` and `*.dump`. The drill
  script itself was unchanged. The wrapper's real (non-stubbed) dump
  path was also exercised separately against a scratch local DB
  (2-row table, 1,333-byte dump, SHA-256
  `76177c67914db1fd6a83d258348a4ab398d52a82ab4af3a5ff91329fd4ee3afe`,
  `pg_restore --list` verified, retention ran with 0 pruned).
- **What this re-run proves:** the loop still passes on the current
  branch after the tooling changes, with fresh numbers — the earlier
  result was not reused. **What it still does NOT prove:** anything
  about the production Railway database (§7 items 4–5, 8).

**How to re-run the drill** (any time, safe — local only):

```bash
cd <repo>
.venv/bin/python scripts/backup_restore_drill.py [--keep] [--dump-file PATH]
```

The script refuses any host that is not localhost and any database
name not ending in `_drill`. It migrates 001..032, seeds synthetic
data, dumps, restores into a fresh database, and compares row-for-row.
Re-run it **monthly**, and after any change to migrations 029–032 or
to the backup script. Record each run in
`docs/harness-railway-checklist.md` §7. Keep the drill artifacts
under the worktree (pass `--dump-file drill/backup-<stamp>.dump` and
save the log as `drill/drill-<date>.log`) — the default dump path is
`/tmp`, which does not survive a machine rebuild; `*.dump` is
gitignored, so the dump itself is not committed, only its SHA-256 in
this doc and the log.

---

## 7. What still needs Ren / Railway access (BLOCKED until observed)

These cannot be verified from the repo. Production backup
verification stays **BLOCKED** until each is observed in the
Railway dashboard for project `gracious-expression` and recorded in
`docs/harness-railway-checklist.md` §7. Eight items (item 8 added
2026-11-08 during launch re-verification).

1. **Volume backups:** production Postgres service → Backups tab.
   Are daily/weekly/monthly schedules enabled? What is the most
   recent snapshot's date/time? If there is no Backups tab/option,
   record "automated backups not available on the current plan".
2. **PITR:** same tab. Is PITR enabled? If yes, what is the restore
   window start (first base backup date)? If no, enable it — the
   window starts at enablement, so every day of delay is a day of
   unrecoverable history. Note: enabling requires the official
   Railway Postgres image with a major version tag (`:16`); confirm
   the production service uses it.
3. **Postgres image/version:** confirm the production Postgres
   service's image and version (Railway dashboard → service
   Settings). Needed because PITR/HA require the official image.
4. **First evidenced pre-deploy `pg_dump`:** take one (§3) before
   the next production deploy; record date, size, SHA-256, storage
   location. **Storage location is part of the check:** name the
   exact place the dump lives (encrypted local disk path, or the
   S3-compatible bucket + object key) and confirm it is outside the
   Railway project and outside the app container. Retention per §2:
   keep every pre-deploy dump for 30 days, and keep the most recent
   pre-deploy dump indefinitely until superseded by a verified newer
   one — do not delete the latest dump to make room.
5. **Restore exercised against a real production dump:** restore
   the §7.4 dump into a scratch Postgres (Railway or local) and
   record exit status + compared row counts. The §6 drill proves
   the loop works on synthetic data; only this step proves it works
   on the real database. See item 8 for the exact scratch procedure
   — a dump of production restored into the wrong target is itself
   an incident.
6. **Cron-service decision (§4):** approve or decline the automated
   offsite dump service, and the retention in §2.
7. **Deployment retention (rollback, related):** confirm the
   pre-Phase-1 deployment is still listed/redeployable (see
   `docs/harness-railway-checklist.md` R9) — not a backup item, but
   the other half of recovery.
8. **Restore-to-scratch procedure for a real production dump
   (added 2026-11-08):** when item 5 is done, follow §5c/§5d
   exactly — restore into a **scratch** database that nothing else
   points at, never into production; verify with the §5d checks
   (migration stamps 001..032, table count, row counts on `users`,
   `harness_settings`, `harness_jobs`, `harness_audit`,
   `harness_costs`, `harness_approvals`, `tenant_sms_senders`,
   `leads`, `tasks`, `sms_messages`, tenant-isolation spot checks);
   record date, restore layer, target, `pg_restore` exit status,
   and compared counts in the checklist §7 log; **then delete the
   scratch database/service** so a stale copy of production data
   does not linger, and confirm the production `web`/`worker`
   services were never pointed at it. A scratch restore that is
   left running is a second production database nobody is watching.

---

## 8. Quick reference (copy-paste, placeholders only)

```bash
# Backup (before every deploy):
pg_dump "$DATABASE_URL" --format=custom --no-owner --file="backup-$(date +%Y%m%d-%H%M%S).dump"
# Wrapper (verifies + hashes; --confirm-production is required for a
# production-looking URL — see §3): scripts/backup_postgres.sh --confirm-production

# Verify a dump file:
pg_restore --list backup-YYYYMMDD-HHMMSS.dump | head

# Restore into a scratch DB:
pg_restore --clean --if-exists --no-owner --dbname="<scratch DATABASE_URL>" backup-YYYYMMDD-HHMMSS.dump

# Railway CLI (after railway link):
railway postgres pitr status
railway postgres pitr enable
railway postgres pitr backup create
railway postgres pitr backup list
railway postgres pitr restore --at "2026-11-08 14:30"
railway connect postgres --tunnel-only   # local tunnel for pg_dump
```

**Never** run `pg_restore --clean` against the production
`DATABASE_URL`. **Never** paste a real `DATABASE_URL` into chat, a
ticket, or this repo.
