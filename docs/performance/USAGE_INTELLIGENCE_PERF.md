# Usage intelligence: performance budgets and indexes

## Why

The recommendation runs on the subscription page, which customers open far more often than
they renew. A model that is correct but slow - or that quietly degrades into a table scan as
the install grows - is a regression the moment there are hundreds of thousands of usage rows.
So the cost is bounded by construction and measured at scale.

`scripts/benchmark_usage_intelligence.py` seeds a full dataset, runs the real code path, and
**exits non-zero** when a budget breaks.

## Budgets

| Budget | Value | Measured (16,000 accounts, 496,000 UsageDaily rows) |
|--------|-------|------------------------------------------------------|
| recommendation latency p95 | < 50 ms | **20.9 ms** (mean 15.5, p50 15.2, max 24.3) |
| DB statements per recommendation | <= 6 | **3** |
| X-UI / outbound HTTP calls | 0 | **0** |
| full scans of `UsageDaily` / `RenewalEvent` | none | **none** (three indexed SEARCH plans) |

At 200 accounts and 6,200 rows the same measurement is p95 20.1 ms: the latency is flat in
table size because every query is scoped to one account, so the recommendation is O(rows in
*this* account's window), not O(rows in the table).

## Where the cost goes

One analysis issues exactly three statements (RFP section 35):

1. the latest **verified** renewal or package change for the account (the cycle boundary);
2. the account's `UsageDaily` rows from the start of the wider of the rolling window and the
   pre-cycle baseline;
3. the latest `UsageCounterState` row.

Everything after that is arithmetic over the loaded rows: the per-window slices, the trend,
the forecast, the confidence and the package choice are pure functions (RFP sections 63-65).
The live counter is passed in by the caller from the in-process snapshot, so no panel call can
happen - and the benchmark asserts that by failing any outbound HTTP request.

## Indexes (RFP sections 30-31)

`renewal_events` (revision `b8d2e3f4a5c6`):

| Index | Columns | Serves |
|-------|---------|--------|
| `ix_renewal_events_server_sub_verified_renewed` | `server_id, sub_id, verified, renewed_at` | the cycle-boundary lookup |
| `ix_renewal_events_server_sub_renewed` | `server_id, sub_id, renewed_at` | the cycle history / dedup window |
| `ix_renewal_events_operation_id` | `operation_id` | idempotent replay lookups |
| `uq_renewal_events_operation_type` | `operation_id, event_type` (unique) | one event per operation and type |

`usage_daily` (existing `uq_usage_daily_server_sub_date`, unique):

| Index | Columns | Serves |
|-------|---------|--------|
| `uq_usage_daily_server_sub_date` | `server_id, sub_id, usage_date` | every window query: account equality plus a `usage_date` range |
| `ix_usage_daily_sub_date` | `sub_id, usage_date` | cross-account/day lookups |
| `ix_usage_daily_usage_date` | `usage_date` | retention pruning and day scans |

The window query pushes the day bound into the indexed column (`usage_date >= :start`) and
then filters the loaded rows on their exact observation timestamps in memory, which is why the
plan is a `SEARCH ... USING INDEX` with a range - not an account-wide scan:

```
plan latest_verified_renewal  SEARCH renewal_events USING INDEX ix_renewal_events_server_sub_verified_renewed (server_id=? AND sub_id=? AND verified=?)
plan usage_since_cycle        SEARCH usage_daily USING INDEX sqlite_autoindex_usage_daily_1 (server_id=? AND sub_id=? AND usage_date>?)
plan rolling_31d              SEARCH usage_daily USING INDEX sqlite_autoindex_usage_daily_1 (server_id=? AND sub_id=? AND usage_date>?)
```

(`sqlite_autoindex_usage_daily_1` is SQLite's name for the unique
`(server_id, sub_id, usage_date)` constraint.)

## Checking it on PostgreSQL (RFP section 51)

SQLite proves the shape; production runs PostgreSQL, so the same three queries must be
`EXPLAIN ANALYZE`d there before a release that changes them:

```sql
EXPLAIN ANALYZE
SELECT * FROM renewal_events
 WHERE server_id = 42 AND sub_id = 'acct'
   AND verified IS TRUE AND event_type IN ('renewal', 'package_change')
 ORDER BY renewed_at DESC, id DESC LIMIT 1;

EXPLAIN ANALYZE
SELECT * FROM usage_daily
 WHERE server_id = 42 AND sub_id = 'acct'
   AND usage_date >= DATE '2026-08-13'
 ORDER BY usage_date ASC LIMIT 400;

EXPLAIN ANALYZE
SELECT * FROM usage_counter_state WHERE server_id = 42 AND sub_id = 'acct';
```

Accept: `Index Scan` / `Index Only Scan` (or a `Bitmap Index Scan` on the composite index) with
a row estimate close to the account's own rows. Reject: `Seq Scan on usage_daily` or
`Seq Scan on renewal_events`, and any plan whose estimated rows grow with the install rather
than with the account.

## Reproduction

```
python scripts/benchmark_usage_intelligence.py --quick          # 200 accounts, ~15 s
python scripts/benchmark_usage_intelligence.py \
    --accounts 16000 --samples 40 --json docs/performance/usage-intelligence.json
```

The committed artifact `docs/performance/usage-intelligence.json` is the 16,000-account run
(496,000 daily rows). `tests/test_usage_intelligence_performance.py` re-runs the quick
benchmark and fails on any budget or plan regression.

## Limits

* SQLite, single process, no concurrent writers: the numbers are the *shape* (flat in table
  size, three statements, indexed reads), not a capacity plan for PostgreSQL.
* The dataset is one renewal per account and 31 days of daily rows, which is the steady state
  the model reads; a longer history is bounded by `MAX_DAILY_ROWS = 400` per window.
* Redis is not exercised here: the recommendation reads the database and the caller's live
  counter, and never Redis directly.
