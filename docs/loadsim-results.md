# TopAI launch load simulation — results (10 / 100 / 1,000 tenants)

Run: 2026-11-08/09 (overnight run, finished ~03:29 MST), worktree
`launch-loadsim`, commit d7af541 base. Script: `scripts/load_sim.py`;
raw numbers: `results/loadsim-run.json`. All providers MOCKED — no real API
calls, SMS, calls, emails, or charges.

## Assumed daily job mix (per tenant, stated explicitly)

| kind | jobs / tenant / day |
|---|---|
| `sms_auto_reply` (inbound SMS coaching) | 8 |
| `scheduled_follow_up` (lead follow-ups) | 12 |
| `execute_approval` (approved-action runs) | 2 |
| **Total** | **22** |

So one day of work = 220 / 2,200 / 22,000 jobs at 10 / 100 / 1,000 tenants.
Deterministic failure injection: every 200th dispatched job fails permanently
(expected dead-letter rate ~0.5%). Mock provider latencies: sms 15 ms,
follow-up 3 ms, approval 8 ms — **~100x faster than a real LLM call**.
Simulated throughput is therefore an **upper bound set by queue/DB overhead,
not by provider latency**; the real-latency projection below is what sizes
workers.

## Measured results (single worker, SQLite, 2 concurrent web threads)

| | 10 tenants | 100 tenants | 1,000 tenants |
|---|---|---|---|
| Jobs enqueued | 220 | 2,200 | 22,000 |
| Enqueue rate | 513 jobs/s (0.43 s) | 412 jobs/s (5.3 s) | 341 jobs/s (64.5 s) |
| Jobs completed | 219 | 2,189 | 11,960 (cap hit) |
| Dead-letter (injected) | 1 (0.45%) | 11 (0.50%) | 60 (0.27% of enqueued; 0.50% of dispatched) |
| Pending at end | 0 | 0 | **9,980** |
| Drained fully? | Yes, 8.1 s | Yes, 40.3 s | **No — 600 s cap hit** |
| Throughput (mock-compressed) | ~97k jobs/hr | ~196k jobs/hr | **~72k jobs/hr** |
| Extrapolated full drain | 8 s | 40 s | ~1,102 s (18 min) |
| Peak queue depth | 220 | 2,200 | 22,000 |
| End queue depth | 0 | 0 | 9,980 |
| Web `/api/harness/status` p50, worker idle | 43 ms | 23 ms | 18 ms |
| Web `/api/harness/status` p50, worker busy | 111 ms | 38 ms | **120 ms** |
| Web `/api/harness/status` p95, worker busy | 161 ms | 79 ms | **218 ms** (max 416 ms) |
| Web `/health` p50, busy | 1 ms | 1 ms | 1 ms |
| Cost-ledger rows written | 220 | 2,200 | 12,020 (= completed + dead-letter) |
| Ledger errors / worker errors / web 5xx | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| SQLite `database is locked` events | 0 | 0 | 0 |

Queue-depth trace (pending vs drain seconds): 100 tenants fell ~linearly
2,200 → 0 in 40 s (~55 jobs/s). 1,000 tenants: 22,000 → 20,373 (50 s) →
19,260 (100 s) → 17,770 (200 s) → 15,130 (300 s) → 12,790 (400 s) →
11,416 (500 s) → 9,980 (601 s) — roughly linear at ~20 jobs/s, no
acceleration as the queue shrank.

## First bottleneck per scale

- **10 tenants — nothing saturates.** Worker drains a day's load in 8 s
  (simulated). First thing to notice is only noise: status-endpoint p95
  (161 ms busy) on a tiny sample, driven by other work on the shared VM
  (see Limitations). No bottleneck at this scale.
- **100 tenants — worker serial throughput.** Queue drains, but only
  because the mock is 100x compressed. At real provider latencies the
  single worker is the bottleneck (see projection). Web latency is barely
  affected (status p50 23 → 38 ms). Ledger and enqueue paths are fine.
- **1,000 tenants — the claim path + SQLite writer lock.** Throughput
  *fell* from ~55 jobs/s (100 tenants) to ~20 jobs/s even though the work
  per job is identical, and the day's queue did not drain in 10 minutes.
  Two concrete causes, both verified in code:
  1. **No `(status, …)` index on `harness_jobs`.** Migration 029 indexes
     `(user_id, created_at)` and `(user_id, status)`, but the worker
     claims cross-tenant (`WHERE status='pending' AND next_attempt_at …
     ORDER BY id LIMIT 1`), so every claim does a full table scan — cost
     grows with queue depth. This is a **real, Postgres-relevant**
     finding, not a SQLite artifact: add
     `CREATE INDEX … ON harness_jobs(status, next_attempt_at, id)`.
  2. **Write amplification + per-query connections.** Each job costs
     ~4 separate transactions on separate connections (claim SELECT,
     claim UPDATE, `complete_job` UPDATE, cost-ledger INSERT), plus a
     `reclaim_stale_jobs` UPDATE every batch, while each
     `/api/harness/status` request fires ~10 separate COUNT queries on
     ~10 separate connections. On SQLite's single-writer lock the web
     reads queue behind worker writes — status p50 rose 18 → 120 ms and
     p95 to 218 ms, while `/health` (no DB) stayed at ~1 ms. The latency
     inflation is mostly a **SQLite artifact** (Postgres MVCC readers
     don't block on writers), but the per-request query count is a real
     inefficiency worth fixing (one query, or a cached status snapshot).

  The cost ledger itself never errored at any scale (12,020 rows at
  1,000 tenants, 0 errors) — ledger writes are not the bottleneck; the
  claim scan and lock contention are.

## Real-latency projection (what actually sizes workers)

Assume realistic provider times: sms reply ~1.5 s (one Anthropic call),
follow-up ~0.3 s, approval ~1.0 s. Provider seconds per tenant-day =
8×1.5 + 12×0.3 + 2×1.0 = **17.6 s**. Measured queue overhead is ~9 ms/job
(small queue) to ~40 ms/job (deep queue) — negligible next to provider
time. So one worker does ≈ 3,600 / 0.8 ≈ **~4,400 jobs/hr** (≈1.2 jobs/s),
and one day's load for N tenants takes **N/200 hours** to drain
(22N jobs ÷ 4,400 jobs/hr).

## Scaling guidance (with the numbers behind each threshold)

- **Second worker: at ~500–800 tenants (mandatory by ~1,600).**
  Drain time = N/200 hours: 500 tenants → 2.5 h, 800 → 4 h, 1,600 → 8 h
  (a full working day with zero headroom for retries, bursts, or a
  worker restart). A morning burst makes it tighter: if 40% of a day's
  inbound SMS lands in 2 hours, a single worker keeps <15 min reply
  latency only up to ~1,000–1,500 tenants. Add worker #2 at 500–800
  tenants; do not run a single worker past ~1,600. Before adding a
  worker, add the `(status, next_attempt_at)` index — a second worker
  doubles claim-scan contention on the same unindexed table, and on
  Postgres two workers need `FOR UPDATE SKIP LOCKED` (already used) plus
  that index to avoid serialising on scans.
- **Postgres connection pooling: when the 2nd worker or 2nd web replica
  arrives (~500 tenants).** Today every `db.get_db()` opens a fresh
  connection per query (no pooling anywhere). At 1,000 tenants the sim
  sustained ~60+ write transactions/s plus ~10 connections per status
  request; Postgres handles that, but per-query connect overhead (~1–5 ms
  each) becomes visible and connection count grows as
  processes × threads. Put a small pool (psycopg pool / PgBouncer,
  5–10 connections) in place before scaling past one worker + one web
  replica, and cut the status endpoint to one query or a short-lived
  cache.
- **Audit/cost-table retention & archiving: start at ~500 tenants;
  mandatory by 1,000.** One ledger row per job = 22 rows/tenant/day →
  100 tenants = 800k rows/yr, 1,000 tenants = **8.0M rows/yr** in
  `harness_costs` alone, plus several `harness_audit` rows per job.
  Monthly partitioning (or monthly archive of rows older than 90 days to
  cold storage) should be in place before the tables pass ~1M rows —
  roughly 6 months at 1,000 tenants, ~15 months at 500. The 60-day
  status counts are covered by the existing `(user_id, created_at)`
  indexes on audit/costs, so reads stay indexed; the risk is table size
  and backup/restore time, not query shape.
- **Enqueue / inbound path: fine to 1,000 tenants.** 341–513 jobs/s
  sustained enqueue; a webhook burst of a few hundred inbound SMS is
  absorbed. No change needed before 1,000 tenants.

## Assumptions and limitations (read before quoting numbers)

- Mocked providers, ~100x time-compressed; simulated jobs/hr are upper
  bounds. Real throughput is provider-latency-bound (~4,400 jobs/hr per
  worker by the projection above).
- Local SQLite on one shared VM (1.5 shared CPUs, 6 GB), not Railway
  Postgres, not gunicorn (in-process Flask test clients instead).
  SQLite serialises writers; Postgres will behave differently — claim-scan
  cost and per-request query counts transfer, writer-lock web latency
  mostly does not.
- Job mix is an assumption (8/12/2 per tenant/day); if real tenants
  average more inbound SMS, the sms share (the LLM-bound one) dominates
  and worker thresholds move down proportionally.
- Only `GET /api/harness/status` + `/health` were web-loaded; dashboard
  pages, login bursts, and webhook ingress were not measured.
- **Noisy neighbours:** sibling launch-readiness agents were running
  pytest suites and a gunicorn instance on the same VM during this run,
  so absolute latencies (especially the 10-tenant web p95) carry noise;
  the 100→1,000 throughput collapse is a large effect and survives it.
- Dead-letter rate is the injected 0.5% (every 200th job), not a measured
  real failure rate; the dead-letter path itself worked (alerts fired,
  jobs settled, 0 lost).
- `create_users_seconds` (147 s for 1,000 users) is bcrypt password
  hashing in the fixture, not a harness cost.
