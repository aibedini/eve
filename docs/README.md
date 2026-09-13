# Eve documentation index

Every document in this directory tree is listed here, grouped by area. Add a
new document to the matching section in the same commit that introduces it;
tests/test_docs_index.py fails when a document is missing from this index or a
relative link does not resolve.

| Area | Start with |
|------|------------|
| Security model and runbooks | [security/THREAT_MODEL.md](security/THREAT_MODEL.md), [security/INCIDENT_RESPONSE.md](security/INCIDENT_RESPONSE.md) |
| Performance evidence | [performance/BASELINE.md](performance/BASELINE.md), [performance/QUERY_OPTIMIZATION.md](performance/QUERY_OPTIMIZATION.md) |
| Day-two operations | [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md) |
| Releasing | [RELEASE_SECURITY.md](RELEASE_SECURITY.md) |
| BNQO control plane | [bnqo/README.md](bnqo/README.md) |
| UI and design system | [UI_DESIGN_SYSTEM.md](UI_DESIGN_SYSTEM.md) |

## Start here

- [Codebase Memory workflow for all coding agents](AI_CODEBASE_MEMORY.md) - AI_CODEBASE_MEMORY.md

## Security

- [Security Architecture](security/ARCHITECTURE.md) - security/ARCHITECTURE.md
- [Audit trail](security/AUDIT_LOG.md) - security/AUDIT_LOG.md
- [Backup Policy](security/BACKUP_POLICY.md) - security/BACKUP_POLICY.md
- [Certificate monitoring (Eve Doctor)](security/CERTIFICATE_MONITORING.md) - security/CERTIFICATE_MONITORING.md
- [CI required checks and release gating](security/CI_REQUIRED_CHECKS.md) - security/CI_REQUIRED_CHECKS.md
- [Financial data privacy](security/FINANCIAL_PRIVACY.md) - security/FINANCIAL_PRIVACY.md
- [Response security headers](security/HEADERS.md) - security/HEADERS.md
- [Incident: database and receipt artifacts committed to git history](security/INCIDENT_HISTORY_EXPOSURE.md) - security/INCIDENT_HISTORY_EXPOSURE.md
- [Git Credential and Data Exposure Runbook](security/INCIDENT_RESPONSE.md) - security/INCIDENT_RESPONSE.md
- [Key Management](security/KEY_MANAGEMENT.md) - security/KEY_MANAGEMENT.md
- [MFA, sessions and step-up authentication](security/MFA.md) - security/MFA.md
- [Network and TLS policy](security/NETWORK.md) - security/NETWORK.md
- [PostgreSQL hardening](security/POSTGRESQL.md) - security/POSTGRESQL.md
- [Permission-based RBAC](security/RBAC.md) - security/RBAC.md
- [Secret management and key versioning](security/SECRETS.md) - security/SECRETS.md
- [Threat Model](security/THREAT_MODEL.md) - security/THREAT_MODEL.md
- [Upload validation and serving](security/UPLOADS.md) - security/UPLOADS.md

## Performance and measurement

- [Adaptive refresh cadence](performance/ADAPTIVE_REFRESH.md) - performance/ADAPTIVE_REFRESH.md
- [Bounded list responses (pagination contract)](performance/API_PAGINATION.md) - performance/API_PAGINATION.md
- [Performance baseline](performance/BASELINE.md) - performance/BASELINE.md
- [Database connection pool](performance/DB_POOL.md) - performance/DB_POOL.md
- [Delta sync for /api/refresh](performance/DELTA_SYNC.md) - performance/DELTA_SYNC.md
- [Latency SLOs: mutation -> cache -> UI](performance/LATENCY_SLO.md) - performance/LATENCY_SLO.md
- [Load test](performance/LOAD_TEST.md) - performance/LOAD_TEST.md
- [Mutation scale: O(1) in the number of panels](performance/MUTATION_SCALE.md) - performance/MUTATION_SCALE.md
- [Bounded, coalesced panel access](performance/PANEL_LIMITS.md) - performance/PANEL_LIMITS.md
- [Per-server snapshot cache](performance/PER_SERVER_CACHE.md) - performance/PER_SERVER_CACHE.md
- [Hot-path query budget](performance/QUERY_OPTIMIZATION.md) - performance/QUERY_OPTIMIZATION.md
- [Scoped refresh locks](performance/REFRESH_LOCK.md) - performance/REFRESH_LOCK.md
- [Mutation / cache / UI regression suite](performance/REGRESSION_SUITE.md) - performance/REGRESSION_SUITE.md
- [Reseller refresh projection](performance/SERIALIZATION.md) - performance/SERIALIZATION.md
- [Per-server adaptive polling](performance/SERVER_POLLING.md) - performance/SERVER_POLLING.md
- [Live updates over server-sent events](performance/SSE.md) - performance/SSE.md
- [Static asset delivery](performance/STATIC_ASSETS.md) - performance/STATIC_ASSETS.md
- [Subscription response cache](performance/SUBSCRIPTION_CACHE.md) - performance/SUBSCRIPTION_CACHE.md
- [Usage intelligence: performance budgets and indexes](performance/USAGE_INTELLIGENCE_PERF.md) - performance/USAGE_INTELLIGENCE_PERF.md

## Architecture decisions and internals

- [Usage intelligence: telemetry, business events and derived analytics](architecture/USAGE_INTELLIGENCE.md) - architecture/USAGE_INTELLIGENCE.md
- [ADR-0011: Renewal events are business facts, not inferred usage-counter resets](architecture/adr/0011-renewal-events-are-business-facts.md) - architecture/adr/0011-renewal-events-are-business-facts.md

## Operations

- [Operations runbook](OPERATIONS_RUNBOOK.md) - OPERATIONS_RUNBOOK.md
- [Observability: request correlation and HTTP metrics](operations/OBSERVABILITY.md) - operations/OBSERVABILITY.md
- [Data retention](operations/RETENTION.md) - operations/RETENTION.md
- [Build identity and verifiable deploys](operations/BUILD_IDENTITY.md) - operations/BUILD_IDENTITY.md
- [Visual regression: the Subscription page](operations/VISUAL_REGRESSION.md) - operations/VISUAL_REGRESSION.md
- [Background workers](operations/WORKERS.md) - operations/WORKERS.md

## GMweb and SMS gateway

- [GMweb gateway contract](GMWEB_CONTRACT.md) - GMWEB_CONTRACT.md
- [📨 SMS Gateway — Delivery-Confirmation Integration Spec](SMS_GATEWAY_DELIVERY_SPEC.md) - SMS_GATEWAY_DELIVERY_SPEC.md

## BNQO control plane

- [BNQO — API Design (Phase 0)](bnqo/API.md) - bnqo/API.md
- [BNQO — System Architecture (Phase 0)](bnqo/ARCHITECTURE.md) - bnqo/ARCHITECTURE.md
- [BNQO — Data Model & Storage Design (Phase 0)](bnqo/DATA_MODEL.md) - bnqo/DATA_MODEL.md
- [BNQO ↔ eve Integration API Contract (Phase 1, normative)](bnqo/EVE_API_CONTRACT.md) - bnqo/EVE_API_CONTRACT.md
- [BNQO ↔ eve Integration (Phase 1 implementation)](bnqo/EVE_INTEGRATION.md) - bnqo/EVE_INTEGRATION.md
- [BNQO — Implementation Plan (Phase 0–6)](bnqo/IMPLEMENTATION_PLAN.md) - bnqo/IMPLEMENTATION_PLAN.md
- [BNQO — Wire Protocol Design (Phase 0)](bnqo/PROTOCOL.md) - bnqo/PROTOCOL.md
- [BNQO — Bidirectional Network Quality Observatory](bnqo/README.md) - bnqo/README.md
- [BNQO — RFP Digest (normative requirements, condensed from RFC-001)](bnqo/RFP_DIGEST.md) - bnqo/RFP_DIGEST.md
- [BNQO — Security Controls Matrix](bnqo/SECURITY_CONTROLS.md) - bnqo/SECURITY_CONTROLS.md
- [BNQO — Service Level Objectives](bnqo/SLO.md) - bnqo/SLO.md
- [BNQO — Test Strategy](bnqo/TEST_STRATEGY.md) - bnqo/TEST_STRATEGY.md
- [BNQO — Threat Model (STRIDE)](bnqo/THREAT_MODEL.md) - bnqo/THREAT_MODEL.md
- [ADR-0001: Rust for probe agent and secure reflector](bnqo/adr/0001-rust-for-agent-and-reflector.md) - bnqo/adr/0001-rust-for-agent-and-reflector.md
- [ADR-0002: STAMP-derived custom secure UDP probe protocol](bnqo/adr/0002-stamp-derived-udp-probe-protocol.md) - bnqo/adr/0002-stamp-derived-udp-probe-protocol.md
- [ADR-0003: SPIFFE/SPIRE for agent identity, with org-CA fallback](bnqo/adr/0003-spiffe-spire-agent-identity.md) - bnqo/adr/0003-spiffe-spire-agent-identity.md
- [ADR-0004: Telemetry transport — OTLP/gRPC with mTLS via regional collectors](bnqo/adr/0004-otlp-grpc-regional-collectors.md) - bnqo/adr/0004-otlp-grpc-regional-collectors.md
- [ADR-0005: Durable queue — NATS JetStream](bnqo/adr/0005-nats-jetstream-durable-queue.md) - bnqo/adr/0005-nats-jetstream-durable-queue.md
- [ADR-0006: Metrics storage — VictoriaMetrics](bnqo/adr/0006-victoriametrics-tsdb.md) - bnqo/adr/0006-victoriametrics-tsdb.md
- [ADR-0007: Event and analytics storage — ClickHouse](bnqo/adr/0007-clickhouse-event-analytics-store.md) - bnqo/adr/0007-clickhouse-event-analytics-store.md
- [ADR-0008: Agent local WAL — SQLite (WAL journal mode) with a thin spool layer](bnqo/adr/0008-agent-local-wal-sqlite.md) - bnqo/adr/0008-agent-local-wal-sqlite.md
- [ADR-0009: Signed typed-job model — no remote shell, ever](bnqo/adr/0009-signed-typed-job-model.md) - bnqo/adr/0009-signed-typed-job-model.md
- [ADR-0010: Throughput testing — built-in Rust engine primary, ephemeral iperf3 adapter optional](bnqo/adr/0010-throughput-engine-and-iperf3-adapter.md) - bnqo/adr/0010-throughput-engine-and-iperf3-adapter.md

## Process and release

- [Release security](RELEASE_SECURITY.md) - RELEASE_SECURITY.md
- [Telegram Sales and Support Roadmap](TELEGRAM_ROADMAP.md) - TELEGRAM_ROADMAP.md

## UI design system

- [Eve UI design system](UI_DESIGN_SYSTEM.md) - the design-system reference: tokens, theming, components, mobile/RTL, motion, accessibility, anti-patterns
- [the eve-ui agent skill](../.agents/skills/eve-ui/SKILL.md) - the operational contract loaded before any UI change

## Repository-level documents

These live outside this directory and cover deployment rather than the
application internals:

- [README.md](../README.md) - product overview, install and first run
- [DOCKER.md](../DOCKER.md) - container and compose deployment
- [OFFLINE_INSTALL.md](../OFFLINE_INSTALL.md) - air-gapped installation
- [OFFLINE_INSTALL_FA.md](../OFFLINE_INSTALL_FA.md) - air-gapped installation (Persian)
- [CHANGELOG.md](../CHANGELOG.md) - per-release change log
- [RELEASE_NOTES.md](../RELEASE_NOTES.md) - release announcements
- [SECURITY.md](../SECURITY.md) - vulnerability disclosure policy
