# ADR-0005: Durable queue — NATS JetStream

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §4.4 (durable message queue in pipeline), §15 (backpressure, server-side dedup, precise batch ACK), §26 (durable MQ, HA), §27 (Kafka/NATS JetStream durable queue — both permitted)

## Context

The pipeline needs a durable queue between collectors and the stream processor that:
buffers WAL-drain bursts (a 72h agent backlog after an outage), provides per-message
idempotent dedup, supports at-least-once delivery with explicit ACKs and
redelivery, and runs HA with minimal operational weight. RFP §27 explicitly allows
either Kafka or NATS JetStream.

Scale envelope: pilot = 2 agents, a few msg/s; target = 1,000 agents / 10,000 paths.
At Profile-A cadence (summary every 10–30s per path plus HF UDP summaries, host
metrics, events) this is on the order of 10³–10⁴ msg/s average, with incident-driven
diagnostic bursts and WAL drains multiplying that transiently. This is *small* by
messaging standards — operational simplicity and latency matter more than
throughput ceiling.

## Decision

**NATS JetStream** (clustered, 3 nodes, Raft, R=3 streams).

- **Streams**: `MEASUREMENTS_HF`, `EVENTS`, `HOSTMETRICS`, `SECURITY`, `AUDIT` —
  `WorkQueuePolicy` retention, per-stream max age aligned with processing-lag
  tolerance (not long-term storage; storage lives in §14 tier), limits-based
  discard of *oldest* only under extreme overflow with alerting (never silent).
- **Subjects**: `meas.<tenant>.<link>` / `events.<tenant>.<type>` — enables
  per-tenant sharding at scale without re-architecting.
- **Dedup**: publisher sets `Nats-Msg-Id = <agent_id>:<seq_start>-<seq_end>`
  (ADR-0004); JetStream's duplicate window (set to hours, covering reconnect
  storms) gives cheap first-line server-side dedup (RFP §15).
- **Consumption**: stream processors use durable pull consumers with explicit
  ACK, redelivery backoff, and `MaxDeliver` → dead-letter subject to ClickHouse
  for poisoned batches (visible, auditable, never lost silently).
- **HA** (RFP §26): 3-node cluster tolerates one node loss with no message loss;
  rolling upgrades are native. Client failover built into the NATS clients
  (Rust: `async-nats`).
- **Ops footprint**: single static binary, no JVM, no ZooKeeper/KRaft, trivially
  runnable on 3 small VMs at pilot and in K8s at scale.

## Consequences

Positive: order-of-magnitude smaller ops burden than Kafka at this scale; sub-ms
pub latency helps the ingest p95 < 5s target (RFP §25); dedup window + work-queue
semantics map directly onto RFP §15 requirements; subject model fits multi-tenancy.

Negative / risks: JetStream's duplicate window is time-bounded — a WAL drain
arriving *after* the window could re-deliver; mitigated by the stream processor's
per-agent high-water-mark dedup and ClickHouse `ReplacingMergeTree` (ADR-0007), so
end-to-end effectively-once does not depend on the window. JetStream is less suited
than Kafka to multi-hour replay of very large backlogs; accepted because durable
replay sources of truth are the agent WAL and the storage tier, not the queue.
Team must learn NATS monitoring (stream/consumer lag alerts wired into the
`control-plane-failure`/queue-capacity alerts of RFP §17).

## Alternatives considered

- **Kafka**: permitted by RFP §27 and the safer brand for very large streams, but at
  10³–10⁴ msg/s it is pure operational overhead (KRaft quorum, partition planning,
  heavier clients, slower rolling upgrades). Its strengths (huge retention, massive
  consumer fan-out, exact partitioning) are unneeded at 1,000-agent scale. Rejected
  for now; the collector/ingest-gateway boundary (ADR-0004) is the seam where Kafka
  could be substituted later without touching agents.
- **Redis Streams**: RFP §27 restricts Redis to cache/coordination; persistence and
  consumer-group semantics are weaker. Rejected.
- **RabbitMQ**: quorum queues are viable but fan-in per-queue throughput and
  operational model fit less well than JetStream subjects; not in RFP §27's named
  options. Rejected.
