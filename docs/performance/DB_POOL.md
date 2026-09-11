# Database connection pool

## Problem

The engine options were hardcoded in app.py (pool_size 15, max_overflow 5,
timeout 10) with no audit and no way to adapt per deployment. Two consequences:

* Worker demand is workers x (pool_size + max_overflow). Four gunicorn workers
  with that setting can hold 80 PostgreSQL connections; nothing warned when the
  server's max_connections was lower, and the failure mode was a pool timeout.
* Pool exhaustion surfaced as a generic HTTP 500 instead of backpressure, so the
  client retried immediately and made it worse.

## Change

New module `panel/core/db_pool.py`:

* `engine_options(url)` builds the options with dialected defaults (SQLite 5+5,
  PostgreSQL 10+10), environment overrides, `pool_pre_ping` always on, an optional
  LIFO reuse order and PostgreSQL `connect_args` for `application_name` and
  `statement_timeout`. In-memory SQLite keeps its own pool implementation, so pool
  sizing arguments are omitted there instead of raising at engine creation.
* `audit(url, options)` reports the effective configuration,
  `expected_max_connections = workers x (pool_size + max_overflow)`, and a warning
  when that exceeds the optional `EVE_DB_MAX_CONNECTIONS`. app.py logs it once at
  startup.
* `pool_summary(engine)` returns the live pool class/size/checkedin/checkedout and
  is exposed on `GET /api/doctor` as `checks.db_pool`.
* `sqlalchemy.exc.TimeoutError` (pool exhausted) now maps to HTTP 503 with
  `Retry-After: 2` and a generic message instead of a 500. Real server errors are
  untouched.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_DB_POOL_SIZE | 5 (SQLite) / 10 (PostgreSQL) | connections per worker |
| EVE_DB_MAX_OVERFLOW | 5 / 10 | extra connections above the pool size |
| EVE_DB_POOL_TIMEOUT | 10 | seconds to wait for a connection |
| EVE_DB_POOL_RECYCLE | 1800 | recycle connections after this many seconds |
| EVE_DB_POOL_USE_LIFO | 0 | reuse the most recently returned connection first |
| EVE_DB_STATEMENT_TIMEOUT_MS | 0 | PostgreSQL statement_timeout in ms |
| EVE_DB_APPLICATION_NAME | eve | PostgreSQL application_name |
| EVE_DB_MAX_CONNECTIONS | unset | audit number; a warning is logged when the worker demand exceeds it |
| GUNICORN_WORKERS / WEB_CONCURRENCY | 1 | used for the audit |

## Measured

`scripts/benchmark_db_pool.py` (`--json docs/performance/db-pool.json`), 200
checkouts against the same SQLite file:

| Engine | physical connections | wall time |
|--------|----------------------|-----------|
| pooled (application policy) | **1** | 64.8 ms |
| unpooled (NullPool) | 200 | 236.9 ms |

Worker connection demand with the default pool (5 + 5):

| Workers | max connections |
|---------|-----------------|
| 1 | 10 |
| 2 | 20 |
| 4 | 40 |
| 8 | 80 |

Set `EVE_DB_MAX_CONNECTIONS` to the server limit to make the startup audit warn
before an upgrade multiplies the demand past it.

## Tests

`tests/test_db_pool.py`: dialected defaults, PostgreSQL connect_args with and
without a statement timeout, in-memory SQLite, environment overrides with invalid
values falling back, `validate()` rejecting nonsense, the audit's worker demand and
warning, the live pool summary, connection reuse across 25 checkouts (zero new
physical connections), the 503 + Retry-After mapping for a pool timeout, and the
measurement script.
