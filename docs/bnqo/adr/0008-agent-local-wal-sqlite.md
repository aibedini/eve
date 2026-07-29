# ADR-0008: Agent local WAL — SQLite (WAL journal mode) with a thin spool layer

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §15 (all WAL requirements), §4.1 (spool to local disk, at-least-once idempotent delivery, ordered resend), §25 (WAL intact after crash, ≥72h buffer, no logical duplicates), §27 ("SQLite only for local WAL (or custom embedded WAL)")

## Context

Agents must survive control-plane, collector, and network outages — including a
≥72h disconnection and agent crashes/restarts — without losing or duplicating
measurements and without ever filling the OS filesystem (RFP §15). The required
properties are precise: crash safety, per-record checksums, increasing sequence
numbers, storage quota, encryption at rest, backpressure, oldest-eviction, a
priority lane for incident records, batch compression, exponential-backoff retry,
idempotent upload, server-side dedup, precise batch ACK, and ordered resend.

RFP §27 narrows the implementation choice to **SQLite (local WAL only)** or a
**custom embedded WAL**. Note the same RFP §14 forbids SQLite for enterprise
server-side stores — this ADR concerns only the agent-local spool, which §27
explicitly carves out.

## Decision

**SQLite (rusqlite, bundled, WAL journal mode) underneath a small `spool` module**
that owns sequencing, quota, priority, and checksums. A custom append-only segment
WAL was evaluated and rejected (below).

Design:

- **Schema**: `records(seq INTEGER PRIMARY KEY /* = agent sequence_number */,
  priority INTEGER, payload BLOB /* prost-encoded, zstd */, crc32c INTEGER,
  enqueued_at INTEGER)`, plus `meta(key,value)` holding the durable
  `sequence_number` high-water mark and `config_version`.
- **Crash safety**: `PRAGMA journal_mode=WAL; synchronous=FULL;` — record insert
  and the sequence-number increment commit in one transaction; on restart the
  spool verifies CRC32C per record during a recovery scan and quarantines
  (not silently drops) corrupted tails (RFP §25: WAL intact after crash).
- **Sequence discipline**: `sequence_number` is a single monotonic u64 per agent,
  assigned at enqueue; ordered resend replays `ORDER BY seq`; precise batch ACKs
  delete `seq <= acked_hi` only after the collector confirms (RFP §15).
- **Quota & eviction**: configurable byte cap sized for ≥72h at configured rates
  (default 512MB); on overflow, evict oldest *low-priority* records first; the
  priority lane (incident/diagnostic records, Profile C/D outputs) is evicted
  last; the cap is enforced before insert so the agent can never fill the OS
  filesystem (RFP §15). Eviction events are counted and reported once telemetry
  resumes.
- **Encryption at rest**: SQLCipher build (or, where FIPS posture matters,
  page-level AES-256-GCM via a VFS shim) with the key in a root-only file
  (`/etc/bnqo/wal.key`, 0400) — honest "at-rest" protection against offline disk
  reads without pretending to defend against a fully compromised host (recorded in
  the threat model, RFP §22 "compromised agent").
- **Backpressure**: in-memory enqueue is bounded (channel capacity); when the WAL
  is at quota the governor sheds *sampling rate* (drops HF cadence, keeps
  summaries) rather than blocking probe engines — measurement degradation is
  visible via the `agent-unhealthy`/queue-capacity signals (RFP §17).
- **Retry**: upload worker with exponential backoff + full jitter, batch size
  adaptive to observed RTT/loss; idempotent key `(agent_id, seq_start, seq_end)`
  on every batch (ADR-0004).

## Consequences

Positive: battle-tested crash semantics (power-loss safety is SQLite's core design
case) with near-zero code; transactional coupling of sequence-number assignment and
record persistence makes "no gaps, no duplicates after crash" straightforward to
verify (netem/chaos tests, RFP §24); SQL gives quota eviction and priority
requeueing in a few lines.

Negative: SQLCipher key handling adds a secret to manage on every host (scoped,
root-only, rotate-on-enrollment); SQLite write throughput (~tens of thousands
of small inserts/s in WAL mode) is far above agent needs (hundreds/s worst case),
but fsync latency on degraded disks adds tail latency — absorbed by the in-memory
enqueue buffer; SQLite on the agent must never leak into server-side use — enforced
by dependency boundaries (server crates do not depend on the spool module).

## Alternatives considered

- **Custom embedded WAL (append-only segments + index)**: maximum control
  (zero-dependency, exact fsync placement, trivial truncation) but we would own
  crash-recovery edge cases (partial last write, torn pages, index rebuild) that
  SQLite has spent two decades hardening — a poor risk trade for a
  security-adjacent component whose correctness is acceptance-tested (RFP §25).
  Rejected.
- **RocksDB/sled**: heavier binaries, more tuning surface, no priority-query
  ergonomics; sled's maintenance status is uncertain. Rejected.
- **Plain files per batch**: no transactional sequence/ACK handling; replay and
  partial-failure semantics become hand-rolled. Rejected.
