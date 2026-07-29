# BNQO — RFP Digest (normative requirements, condensed from RFC-001)

Project: Bidirectional Network Quality Observatory (BNQO). Enterprise/production-grade system for continuous bidirectional network-quality measurement between an Iran server and outside (foreign) server(s), extensible to 1,000 agents / 10,000 paths. Initial scope: 1 Iran server, 1 outside server, 1 independent control plane.

This digest preserves every normative requirement of the original RFP in compressed form. Section numbers reference the original RFP.

## 1. Goal
Independent per-direction detection of: packet loss, burst loss, latency/RTT, one-way delay, jitter/PDV (RFC 3393), reordering, duplication, micro-outages, route/hop changes, TCP/UDP capacity loss, TCP retransmission, MTU/PMTU issues, fragmentation/MTU black hole, ICMP/UDP/TCP interference, TLS/app-layer interference, path asymmetry, rate-limit/congestion, host-level faults masquerading as network faults. Never rely on ping/MTR/iperf3 alone; final state from multiple independent signals.

## 2. Architecture
- Three planes: Measurement (probe traffic), Control (jobs, identity, policy), Observability (ingest, storage, dashboards, alerts, audit).
- Control-plane outage must NOT stop already-configured tests; agents run autonomously and spool results to local disk (WAL).
- Control plane preferably on a third, independent server/network region.
- Logical layout: Iran server (probe agent + secure reflector + local WAL + host metrics) ↔ outside server (same) → control plane (API gateway, agent identity service, job scheduler, telemetry collector, message queue, stream processor, metrics DB, event DB, object storage, dashboard/alerting) via mTLS telemetry.

## 3. Measurement standards
- Core UDP engine based on STAMP (RFC 8762 ext of TWAMP RFC 5357); optional TWAMP interop.
- Jitter/PDV per IPPM RFC 3393.
- One-way delay requires clock sync quality reporting; if clock offset/uncertainty exceeds limits, store OWD with status `invalid-clock-sync`/`low-confidence`, never as precise value.

## 4. Components
### 4.1 Probe Agent (per server)
- systemd service, dedicated non-root user, minimal caps (CAP_NET_RAW), NO arbitrary shell from control plane, only typed predefined jobs, verifies digital signature of configs, keeps last valid config during CP outage, local WAL, at-least-once idempotent delivery, ordered resend after reconnect, every report carries agent_id+sequence_number+config_version, resource limits (CPU/RAM/disk/bandwidth), watchdog+self-health, monotonic clock for durations, UTC wall clock for events, reports NTP/PTP status/offset/uncertainty.
- Language memory-safe: Rust preferred; Go acceptable with threat model + fuzz tests.

### 4.2 Secure Reflector (per endpoint)
- Answers only registered peers; no UDP reply to unauthenticated packets; short-lived session keys; HMAC/AEAD verification; timestamp/sequence/nonce checks; replay window; rejects old/dup packets; response never larger than request (no amplification); rate + concurrent-session limits; binds only to configured IP/interface; auto-expire sessions; logs invalid/replay/auth-failure stats. No permanent open public iperf3 port.

### 4.3 Control Plane
Agent provisioning, identity issuance/rotation, inventory, link/direction definitions, test profiles, job scheduling, config versioning, policy enforcement, remote diagnostics, approval workflow for heavy tests, tenants/projects/environments, full audit, cert revocation, user access control, alerting, retention management. NO general remote shell.

### 4.4 Telemetry Pipeline
OTLP over gRPC with mTLS recommended; OTel Collector separates agents from storage backend.
Pipeline: Agent → Regional Collector → Durable Message Queue → Validation/Enrichment → Stream Processor → Time-Series Storage → Event/Route Storage → Object Storage → Query API → Dashboard/Alerts. Agents NEVER write directly to a database.

## 5. Measurement methods (FR-MEASURE-*)
- 001 ICMP probe: sent/received/loss%/RTT min/avg/max/p50/p95/p99/stddev/timeouts/TTL/packet size/DSCP/IP version. ICMP is only one signal; ICMP-blocked ≠ service down.
- 002 Secure UDP probe: sent/received, forward/reverse/round-trip loss, burst loss, consecutive-loss length, jitter, one-way delay, RTT, reordering count+distance, duplicates, corrupted, payload integrity, inter-arrival distribution, effective bitrate, clock confidence. Packet fields: protocol_version, session_id, test_id, sequence_number, sender_timestamp, nonce, payload_length, flags, authentication_tag.
- 003 TCP probe on real service ports (panel, reverse, TLS, tunnel): connect success rate, connect time, SYN timeout, refused, RST, retransmits, zero window, handshake timeout, duration, bytes, throughput, error classification; optional kernel TCP_INFO/eBPF.
- 004 TLS probe: TCP connect time, handshake time, protocol version, cipher, cert validity/expiration/chain errors, SNI failure, ALPN, resumption, OCSP, app-data exchange success. Never log private keys/credentials.
- 005 Application synthetic probe: real reverse endpoint, HTTPS health, WebSocket upgrade, gRPC health, test payload + hash verify, TTFB, total response time. Dedicated least-privilege token/account; never real user credentials.
- 006 MTR/route analysis: baseline MTR on controlled interval + diagnostic MTR on incidents (loss, latency spike, route change, service failure). Per-hop: hop number, IP, optional rDNS/ASN, loss, sent, last/avg/best/worst/stddev, route hash, route-change detection, missing hop, destination reached. Mid-hop loss without downstream loss ≠ path failure. Support TCP/UDP/ICMP probes, flow-stable/Paris traceroute.
- 007 MTU/fragmentation: PMTU, IPv4 DF behavior, IPv6 MTU, fragmentation-needed, ICMP too-big, MTU black hole, payload-size threshold, per-direction MTU difference. Probe sizes: 64/128/256/512/1200/1280/1400/1472/near-discovered-PMTU. Rate-limited.
- 008 Throughput: 3 profiles — lightweight capacity sample, scheduled capacity test (off-peak), on-demand diagnostic (RBAC+approval+max rate+duration). Modes: TCP single-stream, TCP limited multi-stream, UDP fixed bitrate, both directions separate and simultaneous, various packet sizes. iperf3 allowed only as adapter: ephemeral sessions, random/controlled ports, firewall restricted to peer, short-lived session token, max bitrate/duration, cleanup after, audited, auto-stop if production traffic harmed.

## 6. Test profiles
- A Continuous Lightweight: light secure UDP probe + TCP connect to real port + ICMP if allowed + jittered intervals + summary every 10–30s, minimal bandwidth.
- B Service Quality: TCP+TLS+HTTP/WS/gRPC, payload integrity, response time, service error classification.
- C Deep Diagnostic (auto on incident, with cooldown): MTR more cycles, multi-protocol traceroute, route comparison, MTU discovery, UDP burst, TCP retransmission analysis, interface/kernel metrics, clock health, DNS test.
- D Scheduled Capacity: time window, duration/rate limits, bidirectional, baseline comparison, auto-stop on production errors.

## 7. Host/kernel metrics (per agent)
CPU usage/steal, load avg, memory pressure, disk pressure, NIC RX/TX bytes/drops/errors, FIFO/carrier errors, queue length, qdisc drops, softnet drops, conntrack usage, socket exhaustion, FD usage, TCP retransmissions, UDP receive errors, kernel buffer errors, agent process health, clock offset/source, NTP/PTP status. Correlate in diagnosis; never declare root cause without conclusive evidence.

## 8. Status model
NOT just up/down. Allowed: healthy, degraded, critical, unreachable, probe-blocked, service-failed, agent-unhealthy, telemetry-delayed, control-plane-unavailable, clock-unsynchronized, unknown, maintenance. Must distinguish: link broken vs agent broken vs hub unreachable vs ICMP blocked vs UDP-only blocked vs destination service failed (network fine) vs clocks unsynced vs insufficient data. Never show `healthy` when data is absent.

## 9. Detection engine
Fixed thresholds, per-service-profile thresholds, rolling windows, consecutive failures, multi-window alerts, baseline comparison, route-change correlation, host-metric correlation, data freshness, confidence score.
Initial thresholds: Warning — loss ≥1% over ≥3 windows, or RTT p95 > baseline+50%, or jitter above profile limit. Critical — loss ≥5%, or consecutive service failure, or complete loss, or repeated micro-outages, or telemetry stale beyond limit.
Separate thresholds for: Web, Real-Time, Voice/Video, Tunnel, Bulk Transfer, Management Panel.
Every alert must carry evidence (direction, loss% vs baseline, UDP/TCP/ICMP confirmation, route change + first differing hop, host-resource evidence, confidence).

## 10. Agent identity security (Zero Trust, NIST SP 800-207)
- SR-IDENTITY-001: static API keys FORBIDDEN. Preferred: SPIFFE ID + SPIRE server/agent, X.509-SVID short-lived, mTLS, node+workload attestation. Alternative: org private CA, per-agent unique cert, TPM 2.0 key storage if available, auto-rotation, revocation, short cert lifetime.
- SR-IDENTITY-002 Bootstrap: one-time enrollment token, few-minutes validity, single-agent, invalidated after use, agent fingerprint + node attributes checked, enrollment audited.
- SR-IDENTITY-003 Authorization: agent may only probe assigned links, connect to defined peers, send its own telemetry, receive its own signed jobs. No arbitrary internet targets.

## 11. API security (OWASP API Top 10)
TLS 1.3, mTLS M2M, OIDC/OAuth2 users, MFA for sensitive roles, RBAC (+ABAC if needed), per-object authorization, strict schema validation, request size limits, rate+concurrency limits, replay protection, nonce, timestamp validation, sequence numbers, idempotency keys, body hash, audit logging, pagination, query complexity limits, timeouts, circuit breaker, secure errors (no stack traces), no secrets in URL/query, restricted CORS, CSP, CSRF protection, Secure/HttpOnly/SameSite cookies, session expiration, admin re-auth for dangerous ops.

## 12. Remote job security
Typed jobs only: RUN_ICMP_PROBE, RUN_UDP_PROBE, RUN_TCP_PROBE, RUN_TLS_PROBE, RUN_MTR, RUN_ROUTE_TRACE, RUN_MTU_DISCOVERY, RUN_THROUGHPUT_TEST, COLLECT_HOST_SNAPSHOT, ROTATE_IDENTITY, UPDATE_SIGNED_CONFIG. Free-form command/shell/arbitrary URL/executable path FORBIDDEN. Job fields: job_id, job_type, agent_id, peer_id, parameters, created_at, not_before, expires_at, max_duration, max_bandwidth, approval_id, config_version, signature. Agent rejects expired/unsigned/duplicate/out-of-policy jobs.

## 13. Data model
Measurement record (minimum fields): measurement_id, tenant_id, project_id, environment, agent_id, peer_id, link_id, direction, test_type, protocol, ip_version, source/destination address+port, started_at_utc, ended_at_utc, duration_monotonic_ns, clock_offset_ms, clock_uncertainty_ms, clock_quality, sequence_start/end, packet_size, dscp, ecn, configured_rate_bps, sent/received/lost packets, loss_percent, burst_loss_count, max_loss_burst, duplicate/reordered/corrupted packets, rtt min/avg/p50/p95/p99/max, one_way p50/p95/p99, jitter_ms, throughput_bps, tcp_retransmissions, mtu, route_hash, status, confidence, error_class, agent_version, reflector_version, config_version, received_at.
Route/hop record: route_id, measurement_id, hop_number, hop_address, asn, hostname, loss_percent, sent, received, latency min/avg/p95/max, jitter, destination_reached. All events have versioned schema.

## 14. Storage
Time-series DB for aggregates; event/analytical store (e.g. ClickHouse) for route changes/job results/errors/MTR hops; object storage for diagnostic bundles (and pcaps if enabled); relational DB (PostgreSQL) for users/tenants/agents/policies/inventory/config/audit. SQLite NOT acceptable for enterprise production.
Retention: raw HF samples 7–14d; 10s aggregates 30d; 1m aggregates 90d; 5m aggregates 1y; hourly multi-year per policy; MTR/route events ≥1y; audit logs per org req (WORM preferred); pcaps default off, very short retention.

## 15. Data integrity / offline
Local WAL: crash-safe, per-record checksum, increasing sequence numbers, storage quota, encryption at rest, backpressure, oldest-eviction policy, priority queue for incidents, batch compression, retry w/ exponential backoff+jitter, idempotent upload, server-side dedup, precise batch ACK, ≥72h configurable buffer. Agent must never fill the OS filesystem.

## 16. Dashboard
Global overview (all links, data freshness, active incidents, agent health, directional status); link detail (Iran→Outside and Outside→Iran separately: loss, RTT percentiles, OWD, jitter, reordering, duplication, throughput, TCP retrans, service success rate, MTU, clock quality); route timeline (MTR hop table, route hash/changes, before/after incident comparison, hop where latency/loss starts); incident view (timeline, evidence, correlated metrics, diagnostic jobs, operator notes, ack, resolution, root cause, audit history); security view (agent identity, cert expiration, auth failures, replay attempts, invalid probes, config signature failures, suspicious jobs, rate-limit events).

## 17. Alerting
Pluggable channels: email, Telegram, Slack, webhook, PagerDuty-like, syslog, SIEM. Dedup, grouping, silence, maintenance windows, escalation. Alerts: complete link failure, directional failure, sustained loss, burst loss, high jitter, latency regression, route change w/ degradation, MTU black hole, service failure, agent offline, telemetry stale, clock unsynced, cert expiring, reflector auth attack, local queue near capacity, control-plane failure.

## 18. Packet capture
Default OFF. If enabled: RBAC+approval only, specified filter, limited duration/volume/snaplen, avoid production data, encrypted with dedicated key, full audit, auto-delete, restricted access, redaction, no secrets/sensitive payloads displayed.

## 19. Runtime hardening
Dedicated user, read-only root FS if possible, NoNewPrivileges, PrivateTmp, ProtectSystem/Home/KernelTunables/KernelModules, RestrictNamespaces, RestrictAddressFamilies, syscall filtering, seccomp, AppArmor/SELinux, minimal caps, network egress policy, CPU/memory/FD limits, restart policy, watchdog. No downloading/running unknown binary plugins at runtime.

## 20. Secrets management
No secrets in source/CLI/logs/URLs/container images. Vault/KMS/secret manager, auto-rotation, minimal scope, audited access, TPM/HSM for sensitive keys where available.

## 21. Supply chain (NIST SSDF, SLSA Build L3 target)
Protected branches, mandatory review, signed commits/verified CI identity, unit+integration tests, race detection, fuzz, SAST, dependency/secret/license/container/IaC scanning, SBOM (CycloneDX/SPDX), build provenance, reproducible builds where possible, artifact signing (Cosign), verification before deploy, immutable releases, rollback, staged rollout, canary.

## 22. Threat model (mandatory, STRIDE or equivalent)
Threats to cover: agent spoofing, cert theft, telemetry replay, metric poisoning, result tampering, route forgery, UDP reflection, throughput-test DoS abuse, shell exec via job, SSRF via arbitrary target, cross-tenant access, credential leakage, config tampering, downgrade attack, expired cert, compromised agent/CP/build pipeline, malicious dependency, time manipulation, disk exhaustion, queue flooding, oversized payload, API enumeration, brute force, privilege escalation, pcap data leakage, insider threat. Per threat: Threat, Asset, Attack Path, Likelihood, Impact, Existing Control, Required Control, Residual Risk, Validation Test, Owner.

## 23. APIs
Agent gRPC: EnrollAgent, RotateIdentity, OpenControlStream, FetchSignedConfiguration, AcknowledgeConfiguration, ReceiveJob, AcknowledgeJob, UploadMeasurementBatch, UploadDiagnosticArtifact, ReportHeartbeat, ReportAgentHealth.
Management REST: /v1/agents (GET, GET one, POST revoke), /v1/links (GET/POST/GET one/PATCH), /v1/links/{id}/summary|measurements|routes|incidents, /v1/diagnostics (POST/GET/cancel), /v1/incidents (GET/acknowledge/resolve), /v1/audit-events. Documented with OpenAPI 3.1 + protobuf.

## 24. Mandatory lab failure scenarios (netem/tc)
Loss 0/0.1/1/5/20/100%, random, burst, each direction separately; latency 20/80/150/300ms, sudden changes, variable, asymmetric; jitter low/moderate/severe/periodic/random; reordering, duplication, corruption, fragmentation, MTU black hole, DSCP change, ECN; ICMP/UDP/TCP/port-only blocking, TLS/DNS/SNI/WebSocket failures; route flap, hop change, ECMP, silent hop, ASN change, mid-hop latency increase; host failures (CPU saturation, memory/disk pressure, interface/qdisc drops, conntrack full, socket exhaustion, agent crash/restart, kernel buffer exhaustion); platform failures (CP/collector/DB/MQ down, network partition, 72h offline agent, duplicate/out-of-order batches, clock drift, cert expiration/revocation, invalid config signature); security (replay, forged cert, invalid HMAC, oversized telemetry, excessive jobs, arbitrary target/SSRF, shell/SQL injection, cross-tenant, stolen session, rate-limit bypass, malicious update artifact).

## 25. Acceptance criteria
Accuracy: reported loss matches reference pcap; duplicate/reordering correctly detected; RTT within documented tolerance vs reference; OWD only with valid clock confidence; throughput within defined error; MTU black hole detectable.
Reliability: WAL intact after crash; no logical duplicates after reconnect; hub outage doesn't stop local measurement; ≥72h buffer; no duplicate job execution; invalid config never replaces valid one.
Security: no telemetry without valid identity; revoked cert rejected immediately/within window; replay rejected+audited; reflector silent to unknown packets; unsigned job not executed; no arbitrary shell; throughput limits enforced; no secrets in logs/dumps/UI; no open critical/high vulns before release.
Performance targets: agent idle CPU <1%, memory <150MB, probe bandwidth limited/configurable, dashboard freshness <15s, ingest p95 <5s, auto recovery after restart. All verified by real benchmarks.

## 26. HA
Multiple collectors, LB, durable MQ, DB replication, encrypted backups + restore tests, object-storage versioning, DR runbook, documented RPO/RTO, rolling upgrade, zero/min downtime, agent compatibility with CP one version ahead/behind.

## 27. Tech stack
Agent/reflector: Rust (preferred), Tokio, rustls, protobuf, gRPC, SQLite only for local WAL (or custom embedded WAL), OTel SDK, systemd, optional eBPF.
Backend: Go or Rust, gRPC + REST, PostgreSQL metadata, TSDB for metrics, ClickHouse-like for analytics, Kafka/NATS JetStream durable queue, OTel Collector, Redis only for cache/coordination.
Frontend: TypeScript, React-like, OIDC, strict CSP, no sensitive tokens in localStorage.
Deployment: Ansible for VMs, Terraform for infra, Docker Compose dev-only, K8s/Nomad for CP at scale, agent on real host network.

## 28–30. Repo layout & phases
Repo: cmd/{agent,reflector,control-plane,ingest-gateway,stream-processor,cli}, internal/{measurement,identity,policy,scheduler,telemetry,storage,security,audit}, proto/, api/, web/, deploy/{ansible,terraform,kubernetes,systemd}, configs/, tests/{unit,integration,e2e,netem,security,performance}, threat-model/, docs/{architecture,adr,runbooks,api,operations}, .github/workflows/.
Phases: 0 design (scope, threat model, protocol, data/identity/storage models, ADRs, SLOs) → 1 measurement core (secure UDP, ICMP, TCP, TLS, MTR, MTU, WAL, bidirectional metrics, clock validation) → 2 control plane (enrollment, rotation, signed config, scheduler, policy, ingest, inventory) → 3 observability (pipeline, storage, dashboard, incidents, alerting) → 4 security hardening (SPIFFE/PKI, runtime hardening, RBAC, audit, secrets, supply chain, signed updates, pentest) → 5 validation (netem matrix, benchmarks, chaos, load, recovery, backup/restore, security acceptance) → 6 production (canary, staged rollout, SLOs, runbooks, monitor-the-monitor, post-deploy review).

## 31. Mandatory rules for implementer
No pseudocode as final output; production-grade code; no real sample secrets; no static agent API keys; no arbitrary remote command; no open permanent iperf3; strict input validation; audit all sensitive ops; security+failure tests alongside code; versioned+signed configs; mTLS M2M; releases with SBOM+signatures; agent autonomous during CP outage; every metric has explicit direction; keep unknown separate from healthy; no OWD without clock confidence; no final score replacing raw metrics; AI-assisted diagnosis advisory only, never root cause without evidence; each phase has build+tests+docs+security review.
