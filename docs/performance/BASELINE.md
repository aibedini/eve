# Performance baseline

Measured on the Phase 10 commit with `scripts/benchmark_baseline.py` before any
performance work. Every later performance phase compares against
`baseline-2.5.120.json`; an optimization without a comparison run is not a
verified improvement.

- report: `baseline-2.5.120.json` (raw JSON, generated 2026-09-11T14:36:57Z)
- app version 2.5.120, measured at commit 2ba12b8 (the commit before the baseline
  was added)
- python 3.11.6, Windows 10 (CPython, SQLite database, Flask test client)
- repeat 7, warmup 2 (latency percentiles over the timed runs)

## Dataset

12 servers, 30 inbounds per server, 50 clients per inbound (18,000 clients in the
snapshot), 5,000 transactions, 3,000 payments, 4,000 client ownerships, 30
packages, 10 bank cards. The dataset is deterministic (fixed seed), so two runs
of the same code produce the same data and comparable timings.

## Results

| scenario | status | mean ms | p50 ms | p95 ms | bytes | SQL |
|----------|--------|---------|--------|--------|-------|-----|
| html_login | 200 | 16.9 | 16.6 | 19.2 | 12.0 KB | 5 |
| html_dashboard | 200 | 34.3 | 34.5 | 35.8 | 524 KB | 11 |
| api_permissions | 200 | 10.9 | 10.5 | 12.3 | 0.3 KB | 3 |
| api_refresh_superadmin | 200 | 539.9 | 538.6 | 559.3 | 8.2 MB | 1 |
| api_refresh_reseller | 200 | 2085.0 | 2040.5 | 2373.9 | 163 KB | 3 |
| api_transactions_page | 200 | 226.5 | 225.3 | 234.8 | 10.6 KB | 65 |
| api_transactions_search | 200 | 296.6 | 293.4 | 319.1 | 10.6 KB | 66 |
| api_payments_page | 200 | 381.4 | 380.7 | 391.4 | 9.8 KB | 86 |
| api_finance_stats | 200 | 148.2 | 148.3 | 168.0 | 0.5 KB | 16 |
| api_bank_cards | 200 | 6.8 | 7.0 | 8.6 | 3.0 KB | 2 |

## What dominates (attribution, measured)

1. **`/api/refresh` for a reseller: 2.09 s.** Only 3 SQL statements, so the cost
   is CPU in the request path: a full `copy.deepcopy` of the ~8 MB snapshot, then
   a scan of every inbound and every client to filter them per reseller. This is
   the target of the delta-sync / per-server / lock-removal phases.
2. **`/api/refresh` for a superadmin: 540 ms for an 8.2 MB JSON body.** Single SQL
   query; the cost is JSON serialization of the shared snapshot.
3. **Finance lists: 226-381 ms with 65-86 SQL statements for a 20-row page.** The
   profiler (`cProfile` on `/api/transactions`) attributes 0.35 s of 0.39 s to
   `Transaction.to_dict()` -> `_format_jalali` -> `format_app_datetime` ->
   `_to_app_timezone`, i.e. roughly 16 ms per row for date formatting alone. That
   path calls `_get_app_tzinfo()` / `_get_app_calendar_name()` per row, each of
   which reads a `SystemSetting` row, and constructs a new `ZoneInfo` object every
   call; the ORM relationship access for `admin` / `card` / `server` adds the rest
   of the statement count.
4. Dashboard HTML render (524 KB) is 34 ms, login 17 ms, bank card list 7 ms: no
   action needed.

## How to reproduce

```
python scripts/benchmark_baseline.py --out docs/performance/baseline-<version>.json
```

Useful options: `--quick` (tiny dataset, smoke test), `--repeat`/`--warmup`,
`--seed`, `--db` (keep the generated database), `--compare <baseline.json>` and
`--fail-on-regression <percent>` (exits non-zero when mean or p95 for any
scenario worsens by more than the percentage).

A later phase should run, for example:

```
python scripts/benchmark_baseline.py --compare docs/performance/baseline-2.5.120.json --fail-on-regression 10
```

## Caveats

- Numbers are from the Flask test client in one process, not a real HTTP server,
  and from SQLite; absolute values are machine specific. Use the deltas between
  reports, not the absolute figure, to decide whether a change helped.
- The dataset is synthetic but shaped like production (the same snapshot keys the
  fetcher writes). Snapshot/response sizes are printed so a change that shrinks
  the payload is visible even when latency is noisy.
- `sql_statements` is the maximum count observed for the scenario and is the
  reliable signal for N+1 regressions.
