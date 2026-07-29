# ADR-0004: Telemetry transport — OTLP/gRPC with mTLS via regional collectors

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §4.4 (OTLP over gRPC with mTLS recommended; collector separates agents from storage; agents never write to a database), §11 (TLS 1.3, mTLS M2M), §15 (backpressure, idempotent upload, dedup, ordered resend), §26 (multiple collectors, LB)

## Context

RFP §4.4 prescribes the pipeline
Agent → Regional Collector → Durable Queue → … → Storage and recommends OTLP over
gRPC with mTLS, with an OTel Collector separating agents from the storage backend.
Agents must never write directly to a database. The open decisions: OTLP/gRPC vs
OTLP/HTTP vs a custom protocol, and collector-per-region vs direct agent→backend
ingest.

Drivers: intermittent Iran↔outside connectivity (the very condition we measure)
demands store-and-forward at the agent and a tolerant ingest edge; telemetry must be
authenticated per-agent and replay-safe (RFP §15, §22 "telemetry replay"); the
pipeline must absorb bursts (a 72h WAL draining after an outage) without dropping.

## Decision

**OTLP/gRPC over mTLS to regional OTel collectors.** No direct agent→backend path
exists.

- **Wire**: OTLP/gRPC (`opentelemetry-otlp` Rust crate, tonic + rustls, TLS 1.3).
  Measurement records, host metrics, and security stats are exported as OTLP
  metrics/logs carrying the full RFP §13 attribute set; diagnostic events (route
  changes, MTR results) go as OTLP logs with versioned schemas. zstd/gzip
  compression on the gRPC channel.
- **Identity**: agent presents its X.509-SVID (ADR-0003); collector terminates mTLS
  and authorizes `agent_id` against the SPIFFE ID (SR-IDENTITY-003).
- **Collector config**: `memory_limiter` → `batch` (timeout 1s, size ~4k records) →
  `filter`/`transform` (drop schema-version mismatches to a quarantine stream) →
  custom/NATS exporter publishing to JetStream with dedup key
  `Nats-Msg-Id = <agent_id>:<sequence_start>-<sequence_end>`; persistent
  `file_storage` extension on the collector's sending queue as second-line buffer.
- **Backpressure**: collector 503/backoff → agent retries with exponential
  backoff + jitter and holds data in the WAL (ADR-0008); ordered resend after
  reconnect preserves sequence continuity (RFP §4.1, §15).
- **Idempotency/dedup**: every batch carries `agent_id + sequence_number range +
  config_version`; dedup enforced at the queue (Nats-Msg-Id window), again in the
  stream processor (per-agent high-water mark in Redis/Postgres), and finally by
  unique constraints in ClickHouse (`ReplacingMergeTree` on `measurement_id`) —
  satisfying "at-least-once idempotent delivery, server-side dedup" (RFP §15, §25:
  no logical duplicates after reconnect).
- **Regional collectors** (RFP §26): ≥2 collectors per region behind an L4 LB;
  agents configured with an ordered endpoint list + failover. At pilot scale the
  "region" is the single control-plane site; regions appear as agents spread
  (§7.2 of ARCHITECTURE.md).
- **Control traffic is separate**: the scheduler/job channel (`OpenControlStream`
  etc., RFP §23) terminates at the API gateway/control services, not at the OTel
  collector, keeping measurement ingest and control failure domains apart.

## Consequences

Positive: vendor-neutral schema evolution (protobuf), language-neutral edge (we can
swap collector implementations), buffering and authz concentrated at a hardened,
horizontally scalable edge, storage credentials never leave the control-plane core
(collector compromise ≠ database compromise; see Trust Boundaries TB2/TB3).

Negative: OTel Collector is Go while our services are Rust — a second runtime to
operate and tune (accepted; it is config, not code); the NATS exporter path is a
contrib component — if it lags, our thin `ingest-gateway` service (already in the
RFP §28 repo layout) takes over OTLP→JetStream publishing with tenant stamping.
OTLP metric points for HF packet data need a custom protobuf message in a
log-like envelope to avoid metric-model impedance — handled in the schema
definitions (proto/ per RFP §28).

## Alternatives considered

- **Direct agent→backend ingest (agents write to a gateway that writes DBs)**:
  violates RFP §4.4 (collector separation), couples agent connectivity to core
  availability, and puts storage credentials one hop from the edge. Rejected.
- **OTLP/HTTP(1.1)**: simpler LB/debug but per-request overhead and weaker
  streaming backpressure semantics; gRPC streaming + flow control fits continuous
  telemetry better. Rejected.
- **Custom binary protocol to our own ingest service**: best possible efficiency but
  reinvents schema/versioning/interop work OTLP gives for free, and RFP §4.4
  explicitly recommends OTLP. Rejected (custom protocol remains only on the probe
  path, ADR-0002).
- **Agent→Kafka directly**: puts the durable queue on the untrusted edge, mTLS +
  ACL management per 1,000 agents on Kafka is heavier, and violates collector
  separation. Rejected.
