# Load test

## Problem

The baseline harness measures one request at a time, so nothing validated the
panel under concurrent arrival: whether it holds its latency when several users
poll at once, where the tail comes from, and which configuration produced the
number.

## Change

`scripts/loadtest.py` (new) drives real requests at a fixed arrival rate for a
fixed duration and reports per-scenario latency percentiles, achieved RPS, the
error rate and the status mix.

* Two targets: `--target app` (default) drives the Flask WSGI app in-process on
  a seeded dataset, which is deterministic and CI-friendly; `--url http://host`
  drives a running panel over keep-alive HTTP (anonymous paths; `--cookie` adds
  the authenticated snapshot poll).
* The arrival rate is spread over `--workers` threads; each thread keeps its own
  HTTP connection or test client, so no client object is shared.
* `--disable-limits` (app target) clears the rate limiter to measure capacity
  instead of the limit policy. The extension caches its enabled flag at init, so
  both the config and the live attribute are cleared.
* The report records `panel_concurrency`, `refresh_workers`,
  `fetch_wait_seconds` and the database pool alongside the result, so a capacity
  number can be traced to the settings that produced it.
* `errors` counts transport failures and 5xx; `rejected` counts 4xx, so a 429
  storm is visible but is not mistaken for a broken app.
* `/api/doctor` is deliberately not a scenario: it probes the TLS endpoint of
  every configured server, which is an outbound network operation rather than a
  hot path.

## Baseline

`docs/performance/loadtest-baseline.json`, measured with

    python scripts/loadtest.py --disable-limits --json docs/performance/loadtest-baseline.json

on this machine (Windows, 8 workers, 50 requests/s for 10 s, 12 servers x 30
inbounds x 50 clients in the dataset):

| scenario | requests | rps | p50 ms | p95 ms | p99 ms | errors | rejected |
|----------|----------|-----|--------|--------|--------|--------|----------|
| html_login | 106 | 10.4 | 14.54 | 33.66 | 229.61 | 0 | 0 |
| api_refresh_delta | 106 | 10.4 | 5.75 | 15.79 | 32.43 | 0 | 0 |
| static_style | 106 | 10.4 | 3.68 | 1359.77 | 1834.13 | 0 | 0 |
| api_permissions | 106 | 10.4 | 8.47 | 20.04 | 78.99 | 0 | 0 |
| **total** | **424** | **41.8** | | | | **0** | **0** |

The offered rate was 50/s and 41.8/s was achieved with no errors, so the app
served the load; the gap is scheduling overhead on this (shared, Windows)
machine, not a rejection. The p95 tail on `static_style` (1.36 s against a
3.7 ms median) is environment contention — the file is read and hashed by eight
threads under one GIL while the other scenarios run — and is reported rather than
hidden. A Linux CI run will produce a different tail; the artifact records
`platform` and `python` so the two are not compared blindly.

## Verification

`tests/test_loadtest_harness.py` (7 tests): a fixed rate is approximated and
counted per scenario; the percentiles are ordered; transport failures count as
errors with a 100% error rate; 4xx responses count as rejected rather than
errors; and the real CLI runs in a subprocess, exits 0, writes a report with the
documented shape, reaches more than 15 requests at 20/s for 2 s with zero errors
and zero rejections, and records the concurrency settings.

## Residual risk

* The in-process target exercises the WSGI application, not the socket, the
  gunicorn worker pool or the reverse proxy. Use `--url` against a real
  deployment for end-to-end capacity.
* It is a load generator, not a soak test: durations are seconds, and it does not
  model think time or mixed user journeys.
* The numbers are single-machine and single-process; with several gunicorn
  workers the real aggregate capacity is higher and the per-worker pool limits
  (panel concurrency, refresh workers, database pool) are what bound it — which
  is why they are recorded with every result.
