# BNQO — API Design (Phase 0)

Status: Design (Phase 0). Normative references are to `RFP_DIGEST.md` sections. Wire protocol details (packet format, batch ACK watermark semantics, signed config) are in `PROTOCOL.md`; storage mapping in `DATA_MODEL.md`. This document defines:

1. **Agent gRPC API** — the 11 RPCs required by §23, with full proto3 IDL, error codes, idempotency, and per-RPC auth.
2. **Management REST API** — every endpoint from §23, OpenAPI 3.1-ready, with JSON schemas, pagination, filtering, error format, idempotency keys, and rate limits (§11).
3. **Job envelope** JSON schema with all §12 fields and the signature scheme.
4. **Status model encoding** per §8 — all 12 states, transitions, and the "unknown ≠ healthy" rule.

Security baseline for everything below (§11): TLS 1.3 only; mTLS for machine-to-machine; OIDC/OAuth2 (authorization-code + PKCE) for users; MFA for sensitive roles; strict schema validation; request size limits (default 4 MiB, measurement batches 16 MiB); no secrets in URL/query; secure errors (RFC 9457, no stack traces); audit logging of every sensitive operation.

---

## 1. Agent gRPC API (`bnqo.agent.v1.AgentControl`)

File: `proto/bnqo/agent/v1/agent_control.proto`. Transport: gRPC over TLS 1.3. All RPCs except `EnrollAgent` require a valid SPIFFE X.509-SVID (§10 SR-IDENTITY-001) presented via mTLS; the SPIFFE ID must match the `agent_id` claim (per-object authorization, §10 SR-IDENTITY-003, §11). `EnrollAgent` is the single bootstrap exception and authenticates with a one-time enrollment token (§10 SR-IDENTITY-002).

```proto
syntax = "proto3";
package bnqo.agent.v1;

import "google/protobuf/timestamp.proto";
import "google/protobuf/duration.proto";
import "bnqo/v1/control.proto";   // SignedConfiguration, JobEnvelope, MeasurementBatch,
                                   // Heartbeat, ClientEvent, ServerCommand (PROTOCOL.md §9)

// ================= service =================

service AgentControl {
  // Bootstrap: exchange a one-time enrollment token for initial identity. (§10-002)
  rpc EnrollAgent(EnrollAgentRequest) returns (EnrollAgentResponse);

  // Request rotation of the agent's workload identity (X.509-SVID re-issue). (§10)
  rpc RotateIdentity(RotateIdentityRequest) returns (RotateIdentityResponse);

  // Long-lived bidirectional control stream: config push, job dispatch,
  // heartbeat, measurement batch upload + ACK. (§7 PROTOCOL, §23)
  rpc OpenControlStream(stream bnqo.v1.ClientEvent)
      returns (stream bnqo.v1.ServerCommand);

  // Pull path for config (used on stream reconnect / after outage). (§4.1)
  rpc FetchSignedConfiguration(FetchSignedConfigurationRequest)
      returns (FetchSignedConfigurationResponse);

  // Explicit acknowledgement for a config fetched via pull. (§4.1)
  rpc AcknowledgeConfiguration(AcknowledgeConfigurationRequest)
      returns (AcknowledgeConfigurationResponse);

  // Pull path for jobs (CP normally pushes over the stream; this is the
  // recovery path listing pending jobs for the caller). (§12)
  rpc ReceiveJob(ReceiveJobRequest) returns (ReceiveJobResponse);

  // Acknowledge/reject a dispatched job, and later report its result. (§12)
  rpc AcknowledgeJob(AcknowledgeJobRequest) returns (AcknowledgeJobResponse);

  // Unary fallback for batch upload when the stream is unavailable. (§15)
  rpc UploadMeasurementBatch(UploadMeasurementBatchRequest)
      returns (UploadMeasurementBatchResponse);

  // Initiate a diagnostic artifact upload; returns a constrained
  // pre-signed object-storage URL. (§14, §18)
  rpc UploadDiagnosticArtifact(UploadDiagnosticArtifactRequest)
      returns (UploadDiagnosticArtifactResponse);

  // Unary heartbeat fallback (stream heartbeats are preferred). (§4.1)
  rpc ReportHeartbeat(ReportHeartbeatRequest) returns (ReportHeartbeatResponse);

  // Structured health/self-diagnostics report (watchdog, resource use). (§4.1, §7)
  rpc ReportAgentHealth(ReportAgentHealthRequest) returns (ReportAgentHealthResponse);
}

// ================= messages =================

message EnrollAgentRequest {
  string enrollment_token = 1;   // one-time, ≤15 min validity, single-agent (§10-002)
  string hostname = 2;
  NodeAttributes node_attributes = 3;   // node attestation evidence
  bytes csr = 4;                   // PKCS#10 CSR for the workload identity
  string agent_version = 5;
  string reflector_version = 6;
}

message NodeAttributes {
  string machine_id = 1;          // /etc/machine-id hash
  bytes tpm_quote = 2;            // optional TPM 2.0 attestation (§10 alternative)
  repeated string network_addresses = 3;
  string kernel_version = 4;
}

message EnrollAgentResponse {
  string agent_id = 1;
  string spiffe_id = 2;
  bytes certificate_chain = 3;    // initial X.509-SVID
  google.protobuf.Timestamp cert_expires_at = 4;
  bnqo.v1.SignedConfiguration initial_config = 5;
}

message RotateIdentityRequest {
  string agent_id = 1;
  bytes csr = 2;                  // new keypair; old key never leaves the host (§20)
  string reason = 3;              // scheduled | job ROTATE_IDENTITY | suspected_compromise
}

message RotateIdentityResponse {
  bytes certificate_chain = 1;
  google.protobuf.Timestamp cert_expires_at = 2;
  google.protobuf.Timestamp old_cert_valid_until = 3;  // short overlap, then revoked (§10)
}

message FetchSignedConfigurationRequest {
  string agent_id = 1;
  uint64 current_config_version = 2;  // CP returns 304-style "unchanged" if equal
}

message FetchSignedConfigurationResponse {
  oneof result {
    bnqo.v1.SignedConfiguration config = 1;
    ConfigUnchanged unchanged = 2;    // current_config_version is still latest
  }
}

message ConfigUnchanged { uint64 config_version = 1; }

message AcknowledgeConfigurationRequest {
  string agent_id = 1;
  uint64 config_version = 2;
  bool applied = 3;
  string reject_reason = 4;      // signature_invalid | rollback | invalid_schema | resource
}

message AcknowledgeConfigurationResponse {}

message ReceiveJobRequest {
  string agent_id = 1;
  uint32 max_jobs = 2;           // client-side flow control (§11 concurrency limits)
  google.protobuf.Timestamp not_before_horizon = 3;  // only jobs starting before this
}

message ReceiveJobResponse {
  repeated bnqo.v1.SignedJobEnvelope jobs = 1;
}

message AcknowledgeJobRequest {
  string agent_id = 1;
  string job_id = 2;             // idempotency: re-ACK of same job_id is a no-op
  bnqo.v1.JobAckStatus status = 3;
  string reason = 4;
  bnqo.v1.JobResult result = 5;  // set when reporting completion
}

message AcknowledgeJobResponse {}

message UploadMeasurementBatchRequest {
  bnqo.v1.MeasurementBatch batch = 1;
}

message UploadMeasurementBatchResponse {
  bnqo.v1.BatchAck ack = 1;      // APPLIED / DUPLICATE / REJECTED + high_watermark
}

message UploadDiagnosticArtifactRequest {
  string agent_id = 1;
  string job_id = 2;             // artifact must belong to an authorized job (§18)
  string artifact_name = 3;      // sanitized; no path separators
  ArtifactKind kind = 4;
  uint64 size_bytes = 5;         // must be ≤ job/class quota
  bytes sha256 = 6;              // integrity binding for the uploaded object
}

enum ArtifactKind {
  ARTIFACT_KIND_UNSPECIFIED = 0;
  ARTIFACT_KIND_HOST_SNAPSHOT = 1;
  ARTIFACT_KIND_MTR_RAW = 2;
  ARTIFACT_KIND_PCAP = 3;        // requires pcap-enabled policy + approval (§18)
  ARTIFACT_KIND_AGENT_LOG_BUNDLE = 4;  // secrets-redacted (§20, §25)
}

message UploadDiagnosticArtifactResponse {
  string upload_url = 1;         // pre-signed PUT, single-object, content-length bound
  string object_key = 2;         // s3 key (DATA_MODEL §7)
  google.protobuf.Timestamp expires_at = 3;  // URL validity ≤ 15 min
}

message ReportHeartbeatRequest {
  bnqo.v1.Heartbeat heartbeat = 1;
}

message ReportHeartbeatResponse {
  google.protobuf.Timestamp server_time_utc = 1;
  bool config_update_available = 2;   // hint to pull / expect push
  bool jobs_pending = 3;
}

message ReportAgentHealthRequest {
  string agent_id = 1;
  uint64 health_seq = 2;             // idempotency
  google.protobuf.Timestamp reported_at_utc = 3;
  AgentHealth health = 4;
}

message AgentHealth {                // §7 host/kernel metrics + §4.1 watchdog state
  double cpu_usage_ratio = 1;
  double cpu_steal_ratio = 2;
  double load1 = 3;
  double memory_pressure_ratio = 4;
  double disk_pressure_ratio = 5;
  double wal_utilization_ratio = 6;
  uint64 wal_evictions_total = 7;
  repeated NicHealth nics = 8;
  double conntrack_usage_ratio = 9;
  double fd_usage_ratio = 10;
  uint64 tcp_retransmissions_total = 11;
  uint64 udp_rx_errors_total = 12;
  ClockHealth clock = 13;
  bool watchdog_ok = 14;
  repeated string active_faults = 15; // agent self-diagnosis, advisory (§31)
}

message NicHealth {
  string name = 1;
  uint64 rx_bytes_total = 2;
  uint64 tx_bytes_total = 3;
  uint64 rx_drops_total = 4;
  uint64 tx_drops_total = 5;
  uint64 rx_errors_total = 6;
  uint64 tx_errors_total = 7;
  uint64 qdisc_drops_total = 8;
}

message ClockHealth {
  string source = 1;                 // ntp | ptp | none
  double offset_ms = 2;
  double uncertainty_ms = 3;
  bnqo.v1.ClockQuality quality = 4;
  bool synchronized = 5;
}

message ReportAgentHealthResponse {}
```

### 1.1 Error codes (gRPC status mapping)

| Condition | gRPC code | Retryable by agent |
|---|---|:---:|
| Bad/expired/already-used enrollment token | `UNAUTHENTICATED` | no |
| SVID expired/revoked, SPIFFE ID mismatch | `UNAUTHENTICATED` | after re-enroll/rotation |
| Agent suspended; probing non-assigned link/peer; job out of policy (§10-003) | `PERMISSION_DENIED` | no |
| Unknown agent_id / job_id / config_version | `NOT_FOUND` | no |
| Duplicate job_id / upload_seq ≤ watermark (idempotent hit) | `ALREADY_EXISTS` (with `BatchAck DUPLICATE` detail for batches) | n/a — success-equivalent |
| Signature invalid, anti-rollback violation, malformed envelope | `INVALID_ARGUMENT` + `reason` field | no |
| Job expired / not yet valid | `FAILED_PRECONDITION` | no |
| Rate/concurrency limits (§11) | `RESOURCE_EXHAUSTED` (+ `RetryInfo`) | yes, honor backoff |
| Payload over size limit | `INVALID_ARGUMENT` (`request_too_large`) | no |
| CP internal error | `INTERNAL` | yes, exp. backoff + jitter |
| CP draining / maintenance | `UNAVAILABLE` | yes |

Rich error details use `google.rpc.Status` with `ErrorInfo{domain="bnqo", reason=...}` and, when applicable, `RetryInfo`. No internal identifiers, SQL, or stack traces leak (§11 secure errors).

### 1.2 Idempotency and auth summary per RPC

| RPC | Auth | Idempotency key | Semantics |
|---|---|---|---|
| `EnrollAgent` | one-time enrollment token (§10-002), IP allowlist optional | `enrollment_token` (single-use) | Second call with same token → `UNAUTHENTICATED` (token invalidated, §10-002). |
| `RotateIdentity` | mTLS SVID | CSR public-key fingerprint | Same CSR replayed → same cert returned, no new issuance. |
| `OpenControlStream` | mTLS SVID | per-message keys (config_version, job_id, upload_seq, heartbeat_seq) | Stream itself is not idempotent; every message on it is (PROTOCOL §7). |
| `FetchSignedConfiguration` | mTLS SVID | — | Read-only, naturally idempotent. |
| `AcknowledgeConfiguration` | mTLS SVID | `(agent_id, config_version)` | Re-ACK no-op; conflicting `applied` value for same version → `INVALID_ARGUMENT`. |
| `ReceiveJob` | mTLS SVID | — | Read-only. |
| `AcknowledgeJob` | mTLS SVID | `job_id` | First ACK wins; duplicate ACK returns success (§25 no duplicate job execution). |
| `UploadMeasurementBatch` | mTLS SVID | `(agent_id, upload_seq)` + per-record `measurement_id` | `DUPLICATE` on redelivery; WAL-safe (§15). |
| `UploadDiagnosticArtifact` | mTLS SVID + job authorization (§18) | `(job_id, artifact_name)` | Same key → same pre-signed target; overwrite forbidden (object-lock). |
| `ReportHeartbeat` | mTLS SVID | `heartbeat_seq` | Stale seq logged, not applied. |
| `ReportAgentHealth` | mTLS SVID | `health_seq` | Stale seq logged, not applied. |

---

## 2. Management REST API (`/v1`)

Base URL `https://cp.example.com/v1`. JSON only (`application/json; charset=utf-8`). Auth: OIDC access token (JWT, RS256) in `Authorization: Bearer`. Every response schema below is written so it can be transcribed 1:1 into OpenAPI 3.1 `components/schemas`.

### 2.1 Conventions

**RBAC roles** (§11, mapped from OIDC `roles` claim):

| Role | Capabilities |
|---|---|
| `viewer` | read links/measurements/routes/incidents/audit (own tenant) |
| `operator` | + create/acknowledge/resolve incidents, run light diagnostics (ICMP/UDP/TCP/TLS/MTR/route) |
| `diagnostician` | + heavy diagnostics (MTU discovery, throughput), pcap requests |
| `approver` | + approve heavy tests / pcaps (§4.3, §18) — separate person from requester for pcap |
| `admin` | + agent enrollment/revocation, link/profile management, identity operations |

OIDC scopes: `bnqo:read`, `bnqo:write`, `bnqo:diagnostics`, `bnqo:approve`, `bnqo:admin`. Each endpoint lists required scope + minimum role; per-object tenant authorization is always enforced server-side (§11 per-object authorization, §22 cross-tenant).

**Pagination** (§11): cursor-based.

```
GET /v1/links?limit=50&cursor=eyJ...
→ 200 { "data": [...], "page": { "next_cursor": "eyJ...", "has_more": true } }
```

`limit` default 50, max 200. Cursors are opaque, signed (HMAC), and encode the sort key; sort is always stable (`created_at DESC, id DESC` unless stated).

**Filtering**: query parameters, allowlisted per endpoint (query complexity limits, §11). Unknown params → `400`.

**Idempotency** (§11): all `POST`/`PATCH` accept `Idempotency-Key: <uuid>`; the server stores `(key, tenant, endpoint) → response` for 24 h and replays the original response on retry (`Idempotency-Replayed: true` header).

**Rate limits** (§11): per-token and per-tenant token buckets; defaults — reads 300 rpm, writes 60 rpm, diagnostics creation 10 rpm, audit reads 60 rpm. `429` + `Retry-After` + RFC 9457 body.

**Error format** — RFC 9457 `application/problem+json` (§11 secure errors):

```json
{
  "type": "https://bnqo.example.com/problems/validation-failed",
  "title": "Validation failed",
  "status": 400,
  "detail": "thresholds.loss_warning_percent must be between 0 and 100",
  "instance": "/v1/links/8f3…/requests/01J…",
  "code": "validation_failed",
  "request_id": "01JXYZ…"
}
```

Standard `code` values: `validation_failed`, `unauthenticated`, `forbidden`, `not_found`, `conflict`, `idempotency_conflict`, `rate_limited`, `payload_too_large`, `precondition_failed`, `internal`. Error `code` strings are stable API surface; `detail` is not.

### 2.2 Agents — `/v1/agents` (§23)

#### `GET /v1/agents`
Scope `bnqo:read`, role `viewer+`.
Query: `environment_id`, `status` (`enrolled|active|suspended|revoked`), `hostname_prefix`, `unhealthy_since` (RFC 3339), `limit`, `cursor`.

```json
// 200
{ "data": [ {
    "agent_id": "uuid", "hostname": "ir-fra-01", "spiffe_id": "spiffe://bnqo/agent/uuid",
    "environment_id": "uuid", "status": "active",
    "agent_version": "0.4.2", "reflector_version": "0.4.2",
    "current_config_version": 1182, "last_heartbeat_at": "2026-07-28T21:40:01Z",
    "cert_expires_at": "2026-07-29T09:12:00Z",
    "created_at": "2026-06-01T10:00:00Z"
} ], "page": { "next_cursor": null, "has_more": false } }
```

#### `GET /v1/agents/{agent_id}`
Scope `bnqo:read`, role `viewer+`. Returns the object above plus `"identities"` (cert history, §10), `"resource_limits"`, `"recent_faults"`. `404` when not in caller's tenant.

#### `POST /v1/agents/{agent_id}/revoke`
Scope `bnqo:admin`, role `admin`, **requires step-up auth** (re-auth, §11 admin re-auth for dangerous ops). Idempotency-Key supported.

```json
// request
{ "reason": "suspected_compromise", "revoke_certificates": true, "revoke_sessions": true }
// 202
{ "agent_id": "uuid", "status": "revoked", "revoked_at": "…Z",
  "certificates_revoked": 2, "audit_event_id": "uuid" }
```

Effect: identity revoked (propagated to SPIRE/CRL within the revocation window, §25), config invalidated, sessions expired. Audited (§4.3).

### 2.3 Links — `/v1/links` (§23)

#### `GET /v1/links`
Scope `bnqo:read`. Query: `environment_id`, `service_class`, `status` (any §8 state), `has_open_incident` (bool), `limit`, `cursor`. Response items: link object (below) **without** thresholds detail.

#### `POST /v1/links`
Scope `bnqo:write`, role `admin`. Idempotency-Key supported.

```json
// request
{ "environment_id": "uuid", "name": "ir-fra-1",
  "peer_a": { "agent_id": "uuid", "address": "203.0.113.10", "port": 18462 },
  "peer_b": { "agent_id": "uuid", "address": "198.51.100.7", "port": 18462 },
  "service_class": "tunnel",
  "profiles": [ { "direction": "a_to_b", "profile_class": "A", "test_type": "udp_probe",
                  "interval_ms": 1000, "packet_size": 256, "dscp": 0 } ] }
// 201 → link object; 409 code=conflict when (environment,name) exists
```

Server validates both peers belong to the environment and are `active`; profile params validated against the §11 schema (packet_size 64–9000, interval ≥ 50 ms, …).

#### `GET /v1/links/{link_id}`
Scope `bnqo:read`.

```json
{ "link_id": "uuid", "environment_id": "uuid", "name": "ir-fra-1",
  "peer_a": { "peer_id": "uuid", "agent_id": "uuid", "address": "203.0.113.10", "port": 18462 },
  "peer_b": { "peer_id": "uuid", "agent_id": "uuid", "address": "198.51.100.7", "port": 18462 },
  "service_class": "tunnel",
  "status": { "a_to_b": "healthy", "b_to_a": "degraded", "updated_at": "…Z" },
  "profiles": [ /* as in POST, plus profile_id, enabled, thresholds */ ],
  "created_at": "…Z", "updated_at": "…Z" }
```

#### `PATCH /v1/links/{link_id}`
Scope `bnqo:write`, role `admin`. JSON Merge Patch (RFC 7386) over `{name, service_class, profiles}`; `profiles` replaced wholesale. Optimistic concurrency via required `If-Match: "<etag>"`; `412 precondition_failed` on mismatch (§11). Changing peers is not allowed — create a new link (keeps measurement history unambiguous).

### 2.4 Link sub-resources (§23)

#### `GET /v1/links/{link_id}/summary`
Scope `bnqo:read`. Current per-direction rollup for the dashboard link detail (§16):

```json
{ "link_id": "uuid", "generated_at": "…Z",
  "directions": {
    "a_to_b": { "status": "healthy", "confidence": 0.98, "window": "300s",
      "loss_percent": 0.0, "rtt_ms": {"p50": 41.2, "p95": 58.9, "p99": 70.1},
      "owd_ms": {"p50": 20.8, "p95": 29.4, "clock_quality": "good"},
      "jitter_ms": 1.7, "reordered": 0, "duplicates": 0,
      "throughput_bps": 18400000, "tcp_retransmissions": 3,
      "service_success_ratio": 1.0, "mtu": 1472, "route_hash": "9f2c…",
      "data_freshness_seconds": 6 },
    "b_to_a": { /* same shape */ } },
  "open_incidents": 1 }
```

OWD block is `null` when `clock_quality` is `invalid_clock_sync` (§3 — never presented as precise).

#### `GET /v1/links/{link_id}/measurements`
Scope `bnqo:read`. Query: `direction` (required), `test_type`, `from`, `to` (RFC 3339, max range 31 d), `granularity` (`raw|10s|1m|5m|1h`, auto-capped by range), `status`, `min_loss_percent`, `limit`, `cursor`. Rows are the §13 measurement record (DATA_MODEL §2.1) as JSON.

#### `GET /v1/links/{link_id}/routes`
Scope `bnqo:read`. Query: `direction`, `from`, `to`, `changed_only` (bool — only observations whose `route_hash` differs from the previous observation), `limit`, `cursor`. Items: route observation with nested `hops[]` (DATA_MODEL §2.2), plus `previous_route_hash` and `first_differing_hop` (computed) for the route-change timeline (§16).

#### `GET /v1/links/{link_id}/incidents`
Scope `bnqo:read`. Query: `status`, `severity`, `from`, `to`, `limit`, `cursor`. Items: incident objects (§2.6).

### 2.5 Diagnostics — `/v1/diagnostics` (§23)

#### `POST /v1/diagnostics`
Scope `bnqo:diagnostics`, role `operator+` (light types) / `diagnostician+` (heavy types). Idempotency-Key supported. Creates a §12 typed job.

```json
// request
{ "job_type": "RUN_MTR",            // §12 closed enum; anything else → 400
  "agent_id": "uuid", "peer_id": "uuid",
  "parameters": { "cycles": 10, "probe_protocol": "udp", "flow_stable": true },
  "not_before": "…Z", "expires_at": "…Z",
  "max_duration_ms": 120000, "max_bandwidth_bps": 0 }
// 202
{ "job_id": "uuid", "status": "pending",
  "approval_required": false, "approval_id": null, "created_at": "…Z" }
```

Rules: `RUN_THROUGHPUT_TEST` and pcap-bearing jobs → `approval_required: true`, status stays `pending` until an `approver` calls `POST /v1/diagnostics/{job_id}/approve` (role `approver`; §4.3 approval workflow). Parameter schemas per `job_type` are strict (unknown keys → 400; §11). Targets are restricted to registered peers of the agent (§10-003 — no arbitrary internet targets, no SSRF, §22).

#### `GET /v1/diagnostics`
Scope `bnqo:read`. Query: `job_type`, `status`, `agent_id`, `link_id`, `from`, `to`, `limit`, `cursor`.

#### `GET /v1/diagnostics/{job_id}`
Scope `bnqo:read`. Full job envelope + `result` (measurement ids, artifact refs, error_class) + audit trail refs.

#### `POST /v1/diagnostics/{job_id}/cancel`
Scope `bnqo:diagnostics`. Idempotent: canceling a terminal job returns its current state (`200`) rather than an error. Only the requester or `admin`.

### 2.6 Incidents — `/v1/incidents` (§23)

#### `GET /v1/incidents`
Scope `bnqo:read`. Query: `status`, `severity`, `link_id`, `direction`, `from`, `to`, `limit`, `cursor`.

```json
{ "data": [ {
    "incident_id": "uuid", "link_id": "uuid", "direction": "b_to_a",
    "severity": "critical", "status": "open",
    "opened_at": "…Z", "closed_at": null,
    "evidence": {                          // §9 mandatory evidence bundle
      "loss_percent": 6.2, "baseline_loss_percent": 0.1,
      "confirmed_by": ["udp_probe", "tcp_probe"], "icmp_blocked": true,
      "route_changed": true, "first_differing_hop": 7,
      "host_evidence": ["peer_b nic rx_drops elevated"],
      "confidence": 0.93 },
    "root_cause": null, "operator_notes": "" } ], "page": { … } }
```

#### `POST /v1/incidents/{incident_id}/acknowledge`
Scope `bnqo:write`, role `operator+`. Body `{ "note": "…" }`. Idempotent (re-ack → `200` current state). Sets `acknowledged_at/by`, audited.

#### `POST /v1/incidents/{incident_id}/resolve`
Scope `bnqo:write`, role `operator+`. Body `{ "root_cause": "…", "resolution_note": "…" }`. `root_cause` is operator-attested; AI-suggested causes, when present, are marked `"advisory": true` and never auto-fill (§31). Idempotent.

### 2.7 Audit — `/v1/audit-events` (§23)

#### `GET /v1/audit-events`
Scope `bnqo:read`, role `operator+` (own-tenant). Query: `actor_type`, `actor_id`, `action_prefix` (e.g. `agent.`), `object_type`, `object_id`, `outcome`, `from`, `to` (max 90 d per query), `limit`, `cursor`. Items: the audit record (DATA_MODEL §4). Rate-limited tighter (60 rpm); export beyond 90 d goes through object-storage audit exports (§14).

### 2.8 Endpoint × auth matrix

| Endpoint | Scope | Min role | Idempotency-Key | Rate limit |
|---|---|---|---|---|
| GET /v1/agents, /v1/agents/{id} | `bnqo:read` | viewer | — | 300 rpm |
| POST /v1/agents/{id}/revoke | `bnqo:admin` | admin + re-auth | yes | 10 rpm |
| GET /v1/links, /{id}, /summary, /measurements, /routes, /incidents | `bnqo:read` | viewer | — | 300 rpm |
| POST /v1/links | `bnqo:write` | admin | yes | 60 rpm |
| PATCH /v1/links/{id} | `bnqo:write` | admin | yes + `If-Match` | 60 rpm |
| POST /v1/diagnostics | `bnqo:diagnostics` | operator/diagnostician | yes | 10 rpm |
| GET /v1/diagnostics[/{id}] | `bnqo:read` | viewer | — | 300 rpm |
| POST /v1/diagnostics/{id}/cancel | `bnqo:diagnostics` | operator (owner/admin) | yes | 30 rpm |
| POST /v1/diagnostics/{id}/approve | `bnqo:approve` | approver | yes | 30 rpm |
| GET /v1/incidents | `bnqo:read` | viewer | — | 300 rpm |
| POST /v1/incidents/{id}/acknowledge, /resolve | `bnqo:write` | operator | yes | 60 rpm |
| GET /v1/audit-events | `bnqo:read` | operator | — | 60 rpm |

---

## 3. Job envelope (§12)

### 3.1 JSON representation (REST + audit view)

```json
{
  "job_id": "018e6f2a-…",                    // UUIDv7, also idempotency key
  "job_type": "RUN_THROUGHPUT_TEST",         // closed enum (§12):
                                             // RUN_ICMP_PROBE | RUN_UDP_PROBE | RUN_TCP_PROBE |
                                             // RUN_TLS_PROBE | RUN_MTR | RUN_ROUTE_TRACE |
                                             // RUN_MTU_DISCOVERY | RUN_THROUGHPUT_TEST |
                                             // COLLECT_HOST_SNAPSHOT | ROTATE_IDENTITY |
                                             // UPDATE_SIGNED_CONFIG
  "agent_id": "uuid",
  "peer_id": "uuid",
  "parameters": {                            // per-type JSON Schema; no free-form fields
    "mode": "tcp_multi_stream", "streams": 4,
    "duration_ms": 30000, "packet_size": 1400 },
  "created_at": "2026-07-28T21:00:00Z",
  "not_before": "2026-07-28T21:05:00Z",
  "expires_at": "2026-07-28T21:35:00Z",
  "max_duration_ms": 60000,
  "max_bandwidth_bps": 20000000,
  "approval_id": "uuid",                     // mandatory for heavy tests (§4.3)
  "config_version": 1182,
  "signature": { "key_id": "cp-sign-2026q3", "alg": "Ed25519", "value": "base64…" }
}
```

### 3.2 Protobuf representation

`bnqo.v1.JobEnvelope` / `SignedJobEnvelope` as defined in `PROTOCOL.md` §9 — identical field-for-field to the JSON above (numbers 1–12 are the §12 fields; the signature is detached in `SignedJobEnvelope`).

### 3.3 Signature scheme

```
canonical  = RFC 8785 (JCS) canonical JSON of the envelope WITHOUT the "signature" member
             (REST path), or protobuf deterministic serialization of JobEnvelope (gRPC path)
sig_input  = "bnqo-job-v1" || 0x00 || canonical
signature  = Ed25519_sign(cp_signing_key, sig_input)
```

- The domain-separation prefix prevents cross-protocol replay (a config signature can never validate as a job signature and vice versa, §22).
- `key_id` references the control-plane signing key set distributed at enrollment; rotation via signed config (PROTOCOL §8).
- **Agent-side acceptance rules (§12, verbatim):** reject when — signature invalid (`REJECTED_SIGNATURE`); `now > expires_at` or `now < not_before` (`REJECTED_EXPIRED`); `job_id` already executed/acked (`REJECTED_DUPLICATE`); type/params outside the agent's policy (unassigned link/peer, bandwidth/duration above caps, missing `approval_id` for heavy tests, `config_version` older than applied) (`REJECTED_OUT_OF_POLICY`). Free-form commands, shells, arbitrary URLs, or executable paths are unrepresentable in the schema and rejected at parse time (§12, §22 "shell exec via job", "SSRF via arbitrary target").

---

## 4. Status model encoding (§8)

### 4.1 The 12 states

Encoded as a closed enum everywhere (REST string, proto `LinkStatus`, ClickHouse `Enum8`, VM `bnqo_link_status` one-hot gauge):

| Code | Enum string | Meaning | Example trigger |
|---:|---|---|---|
| 0 | `healthy` | All active signals within profile thresholds, data fresh | — |
| 1 | `degraded` | Warning-level threshold breach (§9: loss ≥1% over ≥3 windows, RTT p95 > baseline+50%, jitter over profile limit) | sustained 1.5% loss b→a |
| 2 | `critical` | Critical threshold breach (§9: loss ≥5%, consecutive service failure, complete loss, repeated micro-outages, stale telemetry beyond limit) | 100% loss a→b |
| 3 | `unreachable` | All probe types fail toward the peer AND reflector counters confirm no arrivals | link down vs. single-protocol block |
| 4 | `probe_blocked` | One or more probe protocols blocked while others pass (ICMP-blocked ≠ down, §5-001; UDP-only blocked, §8) | ICMP filtered, UDP fine |
| 5 | `service_failed` | Network probes healthy but TCP/TLS/app probes to the real service fail (§8 "network fine") | TLS handshake failure, SNI block |
| 6 | `agent_unhealthy` | Agent watchdog/host metrics fault or agent crash (§7) masquerading detected | CPU saturation, agent restart |
| 7 | `telemetry_delayed` | Measurements arriving late/incomplete (WAL backlog, ingest lag) — measurement path degraded, data incomplete | WAL utilization >80% |
| 8 | `control_plane_unavailable` | Agent running on last valid config during CP outage (§2); measurements continue locally | CP down > heartbeat ×3 |
| 9 | `clock_unsynchronized` | Clock quality `invalid_clock_sync`; OWD suppressed (§3) | NTP unsynced |
| 10 | `unknown` | **Insufficient data to decide** — new link, agent never reported, all windows empty | link just created |
| 11 | `maintenance` | Inside an operator-declared maintenance window; alerts silenced (§17) | planned upgrade |

### 4.2 Transition rules

- Computed per (link, direction) by the detection engine from **multiple independent signals** (§1, §8); the inputs and their weights are configuration of the detection profile, but the following invariants are mandatory:
  1. **`unknown` ≠ `healthy`.** Absent data yields `unknown` (or `telemetry_delayed` when lateness is itself the observed fault) — never `healthy` (§8, §31). `healthy` requires positive evidence: at least one full fresh window from each enabled signal with zero threshold breaches.
  2. `unreachable` requires cross-signal confirmation (UDP + TCP + ICMP all failing, or reflector-side zero-arrival evidence); a single blocked protocol is `probe_blocked` (§8).
  3. `service_failed` requires network-layer probes passing while app-layer probes fail; otherwise the failure class is the network state.
  4. `agent_unhealthy`, `clock_unsynchronized`, `control_plane_unavailable` describe infrastructure state and take precedence over link-quality states when present (a link can't be `critical` *because* its measuring agent is down — that is `agent_unhealthy` + per-direction `unknown` for link quality).
  5. `maintenance` is operator-set (time-boxed) and masks alert dispatch, not status computation; the underlying computed state is stored alongside.
  6. Recovery hysteresis: leaving `critical`/`degraded` requires N=3 consecutive clean windows (§9 consecutive-failure logic applied symmetrically) to prevent flapping.

### 4.3 Precedence for display

When multiple conditions hold, the dashboard shows the highest-precedence applicable state with the others as secondary badges:

`maintenance` > `agent_unhealthy` > `control_plane_unavailable` > `telemetry_delayed` > `clock_unsynchronized` > `critical` > `unreachable` > `service_failed` > `probe_blocked` > `degraded` > `healthy` > `unknown`.

(`unknown` sorts last deliberately: it means "no verdict", and the UI must render it distinctly — gray, not green, §8/§31.)

### 4.4 Proto encoding

```proto
enum LinkStatus {              // values match the table above
  LINK_STATUS_HEALTHY = 0;
  LINK_STATUS_DEGRADED = 1;
  LINK_STATUS_CRITICAL = 2;
  LINK_STATUS_UNREACHABLE = 3;
  LINK_STATUS_PROBE_BLOCKED = 4;
  LINK_STATUS_SERVICE_FAILED = 5;
  LINK_STATUS_AGENT_UNHEALTHY = 6;
  LINK_STATUS_TELEMETRY_DELAYED = 7;
  LINK_STATUS_CONTROL_PLANE_UNAVAILABLE = 8;
  LINK_STATUS_CLOCK_UNSYNCHRONIZED = 9;
  LINK_STATUS_UNKNOWN = 10;
  LINK_STATUS_MAINTENANCE = 11;
}
```

Note: `UNKNOWN = 10`, not `0` — the proto3 zero value is a *positive* state only because the storage/transport layers always set the enum explicitly; producers MUST NOT rely on the default, and consumers MUST treat a missing status field as `LINK_STATUS_UNKNOWN`, never as healthy (§31).

---

## 5. Traceability summary

| Element | Digest basis |
|---|---|
| 11 agent RPCs, exact names | §23 |
| Enrollment token, SVID auth, per-RPC authz | §10, §11 |
| Batch/job/config idempotency | §12, §15, §25 |
| REST endpoints per §23, OpenAPI 3.1-ready | §23, §11 |
| Typed job envelope + Ed25519/JCS signature | §12, §22 |
| 12-state status model, unknown ≠ healthy | §8, §31 |
| Evidence bundle on incidents, advisory AI | §9, §31, §16 |
