# ADR-0007: Event and analytics storage — ClickHouse

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §14 ("event/analytical store (e.g. ClickHouse) for route changes/job results/errors/MTR hops"; MTR/route events ≥1y), §13 (route/hop record, versioned schemas), §16 (route timeline, incident view), §22 (auditability of security events)

## Context

Beyond numeric time series, BNQO produces structured, append-only event data:
per-hop MTR/traceroute records (`route_id, measurement_id, hop_number, hop_address,
asn, hostname, loss_percent, sent, received, latency min/avg/p95/max, jitter,
destination_reached` — RFP §13), route-change events with before/after route
hashes, job execution results, error/classification events, security events
(reflector auth failures, replay attempts, invalid probes — RFP §4.2, §16 security
view), and alert evidence bundles (RFP §9). Retention: MTR/route events ≥1 year
(RFP §14).

Access patterns: time-ordered scans per link, point lookups by `measurement_id`,
route-timeline reconstruction, before/after incident comparisons, ad-hoc analytical
queries during incident diagnosis, and dashboard tables. Classic columnar OLAP
workload with high insert rates and rare deletes (TTL-based). RFP §14 names
ClickHouse as the exemplar.

## Decision

**ClickHouse** as the event/analytics store.

- **Tables** (all with `event_date` + engine partitioning by day, `ORDER BY`
  tuned to access path, `TTL` per RFP §14 — route/MTR ≥ 1y, security events per
  audit policy):
  - `route_hops` — `MergeTree`, `ORDER BY (tenant_id, link_id, started_at_utc,
    measurement_id, hop_number)`.
  - `route_changes` — includes `route_hash_before`, `route_hash_after`,
    `first_differing_hop` (feeds RFP §9 evidence and §16 route timeline).
  - `measurement_events` — job results, diagnostic outcomes, error classes,
    micro-outage events.
  - `security_events` — reflector auth failures/replay/invalid probes, config
    signature failures, suspicious jobs (RFP §16 security view).
  - `alert_evidence` — the full evidence bundle per alert (RFP §9).
- **Idempotent inserts**: `measurement_events`/`route_hops` use
  `ReplacingMergeTree(schema_version)` keyed on the record's unique ID so WAL
  redeliveries (at-least-once, RFP §15) cannot create logical duplicates
  (RFP §25); inserts batched by the stream processor (`async_insert` for small
  flushes).
- **Schema versioning**: every event carries `schema_version` (RFP §13: all events
  have versioned schema); migrations add columns, never rewrite in place.
- **HA path** (RFP §26): single node at pilot (nightly encrypted backups + restore
  tests); at scale `ReplicatedMergeTree` with 2 replicas + 3-node ClickHouse
  Keeper, sharded by tenant if ingest requires.
- **Access control**: dedicated read-only role for the Query API; per-tenant row
  policies enforce cross-tenant isolation (RFP §22 "cross-tenant access").

## Consequences

Positive: order-of-magnitude faster analytical queries than PostgreSQL at this
shape (billions of hop rows over a year are routine); TTL + partitioning give
retention compliance almost for free; columnar compression keeps 1y of MTR data
cheap; SQL interface shortens dashboard and incident-view development.

Negative: another stateful system to operate (backups, Keeper at scale);
ClickHouse is poor at updates/deletes — accepted, events are append-only by design
(GDPR-style deletes, if ever needed, use `ALTER TABLE ... DELETE` mutations with a
runbook); eventual dedup visibility with `ReplacingMergeTree` requires `FINAL` or
`GROUP BY` discipline in queries — encoded in the Query API layer, not left to
dashboard authors.

## Alternatives considered

- **PostgreSQL for events too**: simplest ops (already present for metadata) but
  row storage + B-trees are the wrong shape for billions of hop rows and
  time-range scans; partitioning + BRIN helps but doesn't close the gap, and RFP
  §14 explicitly separates an event/analytical store. Rejected.
- **Elasticsearch/OpenSearch**: strong search but heavier RAM/ops, weaker
  compression and aggregation economics for structured events; search is not a
  stated requirement. Rejected.
- **DuckDB/Parquet on object storage only**: great for cold archives (and we do
  export archives), insufficient for <15s-fresh interactive route timelines
  (RFP §25). Rejected as primary; retained as the cold-export format.
