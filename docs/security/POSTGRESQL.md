# PostgreSQL hardening

## Problem

Phase 16 sized the connection pool. The PostgreSQL deployment itself was still
soft in three places:

* Transport security was whatever the DATABASE_URL happened to say. libpq defaults
  to sslmode=prefer, which silently falls back to a plaintext connection, so an
  operator could believe the database link was encrypted when it was not and
  credentials, subscription data and payment rows travelled in clear text.
* Alembic built its own engine with `engine_from_config` and ignored the
  deployment policy: no TLS mode, no statement timeout, and an anonymous
  `application_name`, so a long `upgrade head` was indistinguishable from any
  other backend in pg_stat_activity.
* A database restart, failover or `max_connections` rejection surfaced as an
  HTTP 500. Clients saw a server bug and retried immediately, while a genuinely
  transient connection loss should be retryable backpressure.

## Change

`panel/core/db_pool.py` gained the PostgreSQL transport and diagnosis pieces:

* `host_of(url)` / `is_local_host(url)` parse the database host without exposing
  credentials.
* `ssl_mode()` validates `EVE_DB_SSLMODE` against the libpq modes; an unknown value
  is ignored rather than passed through to libpq.
* `tls_connect_args()` adds `sslmode` and any of `sslrootcert`, `sslcert`,
  `sslkey` from `EVE_DB_SSLROOTCERT`, `EVE_DB_SSLCERT`, `EVE_DB_SSLKEY` to the
  PostgreSQL `connect_args`, so the policy applies to every engine in the process.
* `audit()` now reports `host`, `sslmode` and `tls_warning`. A remote PostgreSQL
  with no TLS (or only `disable`/`allow`) is a warning; `prefer` is named as a
  silent fallback. Local databases are not flagged, and app.py logs the warning
  once at startup.
* `alembic_engine_options(url)` returns the same options with the pool sizing
  removed (Alembic uses NullPool) and `application_name` set to `eve-migrate`
  (`EVE_DB_MIGRATION_APPLICATION_NAME`). `alembic/env.py` now builds its engine
  with `create_engine` + these options instead of `engine_from_config`.
* `pg_health(engine)` returns server version, max_connections, statement_timeout,
  application_name, this database's backend count and whether the session is
  actually SSL-encrypted (`pg_stat_ssl`). It never raises and is exposed on
  `GET /api/doctor` as `checks.postgres`.
* `is_transient_disconnect(exc)` classifies connection-level failures (closed
  connection, refused, too many clients, failover, SQLite lock) and is used by a
  new `OperationalError`/`DisconnectionError` handler in app.py: transient errors
  become 503 + `Retry-After: 2` after a session rollback, while a malformed query
  or missing table keeps the normal 500 path so a real defect is not disguised as
  backpressure.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_DB_SSLMODE | unset (libpq default) | disable / allow / prefer / require / verify-ca / verify-full |
| EVE_DB_SSLROOTCERT | unset | CA bundle for verify-ca / verify-full |
| EVE_DB_SSLCERT | unset | client certificate |
| EVE_DB_SSLKEY | unset | client private key |
| EVE_DB_MIGRATION_APPLICATION_NAME | eve-migrate | application_name during Alembic runs |

Recommended production settings:

```
DATABASE_URL=postgresql://eve:***@db.internal:5432/eve
EVE_DB_SSLMODE=verify-full
EVE_DB_SSLROOTCERT=/etc/ssl/certs/db-ca.pem
```

## Verification

`tests/test_pg_hardening.py` (22 tests) covers the TLS options and audit, the
host parser, the Alembic option set, the pg_health shape on a broken engine and on
a non-PostgreSQL dialect, the transient classifier (connection loss vs query bug)
and both error-handler branches, plus the doctor payload on a SQLite deployment.

No live PostgreSQL server is required; the tests assert the policy that is handed
to libpq and the classification logic. With a real server, `GET /api/doctor`
reports the effective sslmode and the server facts above.

## Residual risk

* The TLS warning is advisory: nothing refuses to start with a plaintext remote
  database. A hard refusal would break deployments that terminate TLS in a sidecar
  or use a private network, so the warning is logged and surfaced in the doctor.
* `pg_health` runs six catalog queries per doctor call. The endpoint is rate
  limited (30/minute) and operator-only.
* The transient handler trusts the error text. It matches only connection-level
  phrases and is unit tested against the obvious false positives (syntax error,
  missing table, unique violation).
