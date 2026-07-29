# BNQO — Bidirectional Network Quality Observatory

Phase-0 design package for the enterprise-grade bidirectional network-quality
monitoring system (Iran ↔ outside servers), produced from RFC-001 (see
`RFP_DIGEST.md` for the condensed normative requirements).

## Reading order

1. `RFP_DIGEST.md` — binding requirements (condensed from the original RFP)
2. `ARCHITECTURE.md` — system design, three planes, DFD + trust boundaries, HA
3. `adr/` — 10 Architecture Decision Records (Rust, STAMP-derived protocol,
   SPIFFE/SPIRE, OTLP collectors, JetStream, VictoriaMetrics, ClickHouse,
   agent WAL, signed typed jobs, throughput engine)
4. `PROTOCOL.md` — BNQO-UDP v1 wire format, session setup, measurement math,
   control stream, proto3 sketches
5. `DATA_MODEL.md` — entities, PostgreSQL/VictoriaMetrics/ClickHouse/S3
   mapping, retention rollups, agent WAL format
6. `API.md` — agent gRPC IDL, management REST, job envelope + signatures,
   12-state status model
7. `THREAT_MODEL.md` — STRIDE, 29 threats, risk ranking
8. `SECURITY_CONTROLS.md` — 60-control catalogue, systemd hardening,
   compliance cross-reference (OWASP/NIST 800-207/SSDF/SLSA)
9. `SLO.md` — SLIs/SLOs, error budgets, time-to-detect targets
10. `TEST_STRATEGY.md` — test pyramid, full netem scenario matrix (~60 cases),
    accuracy/reliability/security/performance methodology, CI gating
11. `IMPLEMENTATION_PLAN.md` — phases 0–6, sprint breakdowns for P1/P2,
    repo bootstrap, CI stages, milestone demos, legacy-pulse deprecation path

## Key decisions (from `adr/`)

- Agent/reflector in **Rust** (tokio, rustls, tonic/prost)
- **BNQO-UDP v1**: STAMP-derived authenticated probe protocol,
  XChaCha20-Poly1305, no unauthenticated replies, anti-amplification
- **SPIFFE/SPIRE** identity (1h X.509-SVIDs), org-CA fallback behind a trait seam
- Telemetry via **OTLP/gRPC mTLS → regional collectors → NATS JetStream**
- **VictoriaMetrics** (metrics) + **ClickHouse** (events/routes) +
  **PostgreSQL** (metadata) + **S3** (artifacts); SQLite only for agent-local WAL
- **Signed typed jobs only** — no remote shell, no arbitrary targets
- Built-in Rust throughput engine; ephemeral iperf3 adapter optional, off by default

## Open questions for the RFP owner

Surfaced during design (details in `PROTOCOL.md` / `DATA_MODEL.md` /
`API.md` traceability sections):

1. Retention: 10s-aggregate tier vs 10–30s Profile-A windows — extend raw TTL
   or add a dedicated tier (decision deferred to Phase-1 benchmarking).
2. Duplicate-measurement vs replay-rejection conflict — resolved by reflecting
   duplicates with a cap (8/seq) and separate counters; needs ratification.
3. Per-direction loss requires the reflector to report its own counters
   (treated as a full agent) — confirm.
4. Field conventions fixed where §13 was silent (`clock_offset_ms` float64 ms,
   `confidence` 0..1, closed `error_class` taxonomy).
5. Enrollment-token transport to agent hosts is an ops decision (e.g. Ansible).
6. Added `POST /v1/diagnostics/{job_id}/approve` to complete the mandated
   approval workflow missing from the §23 endpoint list.
7. §8 status transitions/precedence supplied as design invariants for review.

## Estimate (from `IMPLEMENTATION_PLAN.md`)

Phases 0–6 total ≈ 14–18 months elapsed, peak 5–6 engineers.
Next step after owner review: Phase 1 repo bootstrap (`cargo` workspace per
§28 layout) and measurement-core sprints.
