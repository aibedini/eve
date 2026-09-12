# Operations runbook

Day-two tasks for a running Eve deployment, using the endpoints and commands built
by the hardening program. Each section says what to look at, what a healthy answer
is, and what to do when it is not.

## 1. Health at a glance

    curl -s -H "Cookie: <session>" https://<panel>/api/doctor | jq .

The endpoint is operator-only (`settings.read`) and rate limited (30/minute). It
returns `state` (the worst check) and:

| check | healthy | what it tells you |
|-------|---------|-------------------|
| database | ok | a trivial query against the configured database |
| disk | ok / warning / critical | free space on the application volume |
| secret_key | ok | `SERVER_PASSWORD_KEY` is configured |
| tls | ok | certificate validity for the panel and its endpoints |
| postgres | ok or "sqlite deployment" | server version, connections, and whether the session is TLS-encrypted |
| http_metrics | ok | in-process request/error counters of this worker |
| audit_chain | ok | the audit hash chain still verifies |
| workers | ok | which background workers this process started or skipped |
| retention | ok | the per-policy window and the last cleanup |
| db_pool | ok | live pool size, checked in/out, overflow |
| panel_limits | ok | panel fetch concurrency and counters |
| refresh_policy | ok | snapshot age and the adaptive interval |
| snapshot_cache | ok | Redis snapshot state and hit counters |
| subscription_cache | ok | rendered-response cache size and hit rate |

`X-Request-ID` is returned on every response; a 500/503 JSON payload carries the
same id, and the server log line includes `request_id=...`, so a user report can be
matched to a log line without guessing.

## 2. Background workers

`PROCESS_ROLE` decides what a process runs: `web` starts only the snapshot reader;
`worker` (and `combined`) start the scheduler, watchdog, rollups, bots, pulse and
BNQO schedulers, each gated by a singleton lock. `/api/doctor` `checks.workers`
lists `state` per worker: `started`, `skipped` (another process owns it) or
`failed` (with the error), plus `singleton_errors` when a lock file could not be
used and the worker failed open. See [operations/WORKERS.md](operations/WORKERS.md).

If a worker is missing everywhere, check the lock directory written by
`panel.core.runtime_files` and the container logs for `[Singleton]` lines.

## 3. Data retention

    python -m panel.services.retention --status
    python -m panel.services.retention --dry-run
    python -m panel.services.retention --only health_logs --batch-size 1000

The scheduler runs the pass at most once every 24 hours. Windows come from
`retention_days_<policy>` (`0` disables a policy) and `retention_enabled`
disables everything. Progress and errors live in the `system_migrations` ledger
under `retention:<policy>`. See [operations/RETENTION.md](operations/RETENTION.md).

## 4. Audit trail

    curl -s -H "Cookie: <session>" "https://<panel>/api/audit-log?action=auth.login.failed&limit=20" | jq .

Each entry carries `request_id`, `source_ip`, `user_agent` and both chain hashes.
`checks.audit_chain` in `/api/doctor` verifies the last 2000 rows; a
`content_mismatch` means a row was edited and `chain_link` means one was deleted.
For a full offline verification run `panel.services.audit.verify_chain()` against a
database copy. See [security/AUDIT_LOG.md](security/AUDIT_LOG.md).

## 5. Performance evidence and regression

    python scripts/benchmark_baseline.py --out /tmp/after.json --compare docs/performance/query-after.json
    python scripts/loadtest.py --disable-limits --rate 50 --duration 10
    python scripts/benchmark_queries.py --json /tmp/profile.json

The baseline harness reports latency percentiles, response size and SQL statement
counts per scenario and fails on a regression above the tolerance; the load test
reports p50/p95/p99 under a fixed arrival rate; the profiler shows which statement
repeats. The measured history lives in `docs/performance/` with a JSON artifact per
phase. See [performance/BASELINE.md](performance/BASELINE.md) and
[performance/LOAD_TEST.md](performance/LOAD_TEST.md).

## 6. Subscription and snapshot caches

`checks.subscription_cache` shows the rendered-response cache hit rate and TTLs;
`EVE_SUBSCRIPTION_CACHE_ENABLED=0` disables it, and cached-client mutations
invalidate the server they touch. `checks.snapshot_cache` shows the Redis state used
by the delta sync in `/api/refresh`. See
[performance/SUBSCRIPTION_CACHE.md](performance/SUBSCRIPTION_CACHE.md) and
[performance/DELTA_SYNC.md](performance/DELTA_SYNC.md).

## 7. Releasing

    python scripts/release_check.py                 # ci profile, every commit
    python scripts/release_check.py --profile release
    gh attestation verify oci://ghcr.io/aibedini/eve:sha-<commit> --owner aibedini

The Docker workflow refuses to publish unless the guard passes, and the pushed
image carries a signed SBOM and provenance. See
[RELEASE_SECURITY.md](RELEASE_SECURITY.md).

## 8. When something is wrong

| symptom | first check | action |
|---------|-------------|--------|
| 503 "Server is busy" | `checks.db_pool`, `checks.panel_limits` | raise `EVE_DB_POOL_SIZE`/`EVE_PANEL_CONCURRENCY` only after checking the server's `max_connections` |
| dashboard stale | `checks.refresh_policy`, worker list | confirm the data fetcher is running somewhere; force a refresh from the UI |
| subscriptions stale for one server | `checks.subscription_cache` | the credential write-through invalidates automatically; a TTL of seconds is expected |
| audit chain warning | `checks.audit_chain` | treat as a possible edit/deletion: preserve a database copy and follow the incident runbook |
| disk filling up | `checks.disk`, retention status | run the dry run, then the retention pass; check backup retention too |
| frequent 500s | `checks.http_metrics` | read the slowest/busiest endpoints, then the log line for their `request_id` |
| gateway sends failing | SMS provider settings, GMweb contract | 401/403 is a key/scope problem, 429 is rate limiting with `Retry-After`, 5xx is retried once with the idempotency key |

Incidents that involve credentials or personal data:
[security/INCIDENT_RESPONSE.md](security/INCIDENT_RESPONSE.md).
