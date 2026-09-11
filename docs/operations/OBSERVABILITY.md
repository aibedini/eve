# Observability: request correlation and HTTP metrics

## Problem

Diagnosing a user report meant searching logs with nothing to search by: there
was no request identifier, so "the page failed at 14:03" was the only handle. The
panel also had no view of which endpoints were slow or failing; the doctor
endpoint reported certificates, the database pool and snapshot caches, but not
the request path itself.

## Change

`panel/core/http_metrics.py` (new): dependency-free, in-process counters.

* `observe(endpoint, method, status, duration_ms)` records one finished request
  into a bucket keyed by `"<METHOD> <endpoint>"` — never by raw path, so a scanner
  hitting random URLs cannot grow the map; anything without an endpoint (404s,
  static files, unmatched routes) lands in one bucket.
* `snapshot(limit)` returns uptime, total requests, tracked endpoints, server
  errors, error rate, the slow-request count, the status-class spread, and the
  `busiest` and `slowest` endpoints with mean/max latency, last status and last
  seen time.
* `MAX_KEYS = 200` bounds memory by evicting the least recently used endpoint;
  `SLOW_REQUEST_MS = 1000` defines the slow counter.
* `reset()` clears everything for tests and diagnostics.

`app.py`:

* `_security_per_request_setup` sets `g.request_id` and `g.request_started`. An
  inbound `X-Request-ID` is honoured only after sanitising it to
  `[A-Za-z0-9._:-]` and 64 characters, so a crafted header cannot inject
  anything into a log line or the response; otherwise a random 16-hex id is
  minted.
* `add_security_headers` adds `X-Request-ID` to every response and feeds the
  metrics hook. Because it runs for error responses too, the counters see the
  real status mix.
* The 500 handler logs the id (`request_id=...`) and JSON error payloads carry
  `request_id` (500 from `internal_server_error`, 503 from
  `_service_unavailable`), so a user can quote it.

`GET /api/doctor` exposes the snapshot as `checks.http_metrics` and marks it
`warning` when the process error rate exceeds 5%.

## Verification

`tests/test_observability.py` (12 tests): every response carries an id; two
requests differ; a safe inbound id is echoed; a hostile one is sanitised to the
allowed character set; the 500 payload returns the id set on the request; the
snapshot starts empty; requests are counted per endpoint with 404s as client
errors (not server errors); a 500 raises the error rate; slow requests are
counted; the map stays bounded when `MAX_KEYS` is exceeded; `observe` tolerates
garbage input; and `/api/doctor` reports the metrics block.

## Residual risk

* The counters are per process and reset on restart. With several gunicorn
  workers each has its own view; `/api/doctor` answers for the worker that
  served the request. That is deliberate (no Redis round trip on the hot path),
  but it is not a replacement for a real metrics stack.
* Only mean, max and a slow count are kept — no percentiles. They identify a
  problem endpoint; they are not a latency SLO.
* `X-Request-ID` is returned to the client. It is a random token or a sanitised
  client value, never a session or user identifier.
