# ADR-0006: Metrics storage — VictoriaMetrics

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §14 (time-series DB for aggregates; retention tiers), §25 (ingest p95 <5s, dashboard freshness <15s), §26 (DB replication, rolling upgrade), §27 (TSDB for metrics)

## Context

The TSDB holds high-frequency measurement aggregates (per-direction loss/RTT/OWD/
jitter/reordering percentiles per link per 10s), host/kernel metrics (RFP §7),
pipeline self-metrics, and alert-rule inputs. Retention is tiered (RFP §14): raw HF
7–14d, 10s 30d, 1m 90d, 5m 1y, hourly multi-year.

Cardinality estimate at target scale: 10,000 paths × ~40 series per path per
direction × 2 directions ≈ 800k active measurement series, plus ~1,000 agents ×
~150 host series ≈ 150k, plus pipeline/self-metrics — roughly 1M active series,
growing with tenants/labels. Candidates per RFP §27's "TSDB for metrics":
Prometheus (with remote-write + Thanos/Cortex for HA/long-term), Mimir, or
VictoriaMetrics.

## Decision

**VictoriaMetrics** — single-node at pilot, **vmcluster**
(vminsert/vmselect/vmstorage, replication factor 2) at scale.

- **Ingestion**: stream processor writes Prometheus remote-write (also accepts
  native OTLP metrics — kept as an option to shorten the path); ingest p95 < 5s
  budget (RFP §25) is easily met at this volume.
- **Downsampling/retention**: native per-retention downsampling rules implement the
  RFP §14 tiers verbatim (raw→10s→1m→5m→hourly with distinct retention windows),
  without external compaction jobs.
- **Query**: MetricsQL (PromQL superset) behind the Query API; the dashboard's
  directional link views query per-direction label sets (`direction="iran_to_out"` /
  `"out_to_iran"` — explicit direction is mandatory per RFP §31).
- **Alerting inputs**: vmalert evaluates the fixed/profile thresholds of RFP §9 for
  metrics-derived alerts; the stream processor remains the authority for
  evidence-bundle alerts (route/host correlation, RFP §9), so vmalert covers the
  threshold subset only.
- **HA** (RFP §26): vmcluster RF=2 across nodes/AZs, rolling upgrades native;
  single-node pilot protected by encrypted backups + restore tests (RFP §26).
- **Self-observability**: VictoriaMetrics also scrapes collector/JetStream/processor
  metrics (monitor-the-monitor inputs, RFP §30 phase 6).

## Consequences

Positive: best compression and lowest RAM/disk per active series of the candidates
(comfortable headroom for 1M+ series on modest VMs); single binary at pilot with a
linear scale-out path; native multi-tier retention maps 1:1 to RFP §14; PromQL
compatibility keeps the dashboard and runbook queries portable.

Negative: smaller ecosystem than Prometheus (community dashboards/exporters need
adaptation — cosmetic); cluster mode adds three component types to operate at
scale (accepted, standard K8s manifests exist); MetricsQL extensions are a mild
lock-in — mitigated by restricting dashboards to the PromQL-compatible subset.

## Alternatives considered

- **Prometheus (single + remote-write to long-term store)**: the reference
  implementation, but single-node is an HA dead-end (RFP §26 requires DB
  replication), and pairing it with Thanos/Cortex for HA/long-term reintroduces
  exactly the multi-component complexity we avoid with VictoriaMetrics. Rejected as
  the system of record (Prometheus exposition format still used everywhere).
- **Grafana Mimir**: horizontally scalable to far beyond our needs; correspondingly
  heavy (object-storage dependency, many microservices) at pilot scale. Strong
  candidate if scale outgrows vmcluster; rejected now on operational weight.
- **ClickHouse as the only metrics store**: possible (it stores events anyway,
  ADR-0007) but doubles the write amplification for HF numeric data and lacks
  PromQL ergonomics for alerting; RFP §14 explicitly separates "time-series DB for
  aggregates" from the event store. Rejected.
