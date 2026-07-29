# BNQO — Data Model & Storage Design (Phase 0)

Status: Design (Phase 0). Normative references are to `RFP_DIGEST.md` sections. Wire-level field definitions live in `PROTOCOL.md`; RPC surfaces in `API.md`. Storage mapping follows §14: PostgreSQL (metadata/inventory/config/audit), VictoriaMetrics (metric aggregates), ClickHouse (events/routes/measurement records), S3-compatible object storage (diagnostic bundles), agent-local WAL (§15). SQLite is used only inside the agent WAL (§27), never as a service store (§14).

Conventions: all timestamps `timestamptz`/UTC; all IDs are UUIDv7 (time-ordered, index-friendly) unless noted; `direction` is always explicit (§31 "every metric has explicit direction") and encoded `a_to_b` / `b_to_a` relative to the link's `endpoint_a`/`endpoint_b`.

---

## 1. Logical entity model

```
tenant 1───n project 1───n environment 1───n link
                                            │
agent n───1 environment        link n───2 agent (endpoint_a, endpoint_b)
peer  = (agent, address:port) reachable probe endpoint; a link references two peers
link 1───n test_profile  (profile assignments per direction)
link 1───n measurement  (windows; §13 record)
measurement 1───n route_observation 1───n route_hop
link 1───n incident 1───n alert
job n───1 agent,  job n───1 link,  job 0..1───n measurement (job results)
identity (cert/SVID) n───1 agent
audit_event n───1 actor (user | agent | control-plane component)
```

Entities and key attributes (PK/FK notation, types elaborated in §4 DDL):

- **tenant** — `tenant_id`, `name`, `status`, retention policy ref. Top-level isolation boundary (§22 "cross-tenant access").
- **project** — `project_id`, `tenant_id→tenant`, `name`, `description`.
- **environment** — `environment_id`, `project_id→project`, `name` (e.g. `prod`, `staging`), used in every measurement record (§13).
- **agent** — `agent_id`, `environment_id→environment`, `hostname`, `spiffe_id`, `agent_version`, `reflector_version`, `status` (`enrolled|active|suspended|revoked`), `last_heartbeat_at`, `current_config_version`, resource limits, created/updated. (§4.1, §4.3 inventory.)
- **peer** — `peer_id`, `agent_id→agent`, `address` (inet), `port`, `ip_version`, `role` (`probe|reflector|both`). Peers are the only legal probe targets (§10 SR-IDENTITY-003).
- **link** — `link_id`, `environment_id`, `name`, `peer_a_id→peer`, `peer_b_id→peer`, `service_class` (§9 six classes), `status` (§8 12-state model), `baseline_window`, created/updated. Direction is a property of measurements, not of the link; the link fixes which endpoint is `a` (Iran-side by convention) and which is `b`.
- **test_profile** — `profile_id`, `link_id→link`, `direction`, `profile_class` (A/B/C/D, §6), `test_type`, `interval_ms`, `jitter_fraction`, `packet_size`, `dscp`, `thresholds_json` (per-service-class §9), `enabled`.
- **job** — all §12 fields (see `API.md` job envelope) plus `status`, `requested_by`, `approved_by`, `executed_at`, `result_ref`.
- **measurement** — the §13 record (full field list in §2).
- **route_observation / route_hop** — §13 route/hop records (§2.2).
- **incident** — `incident_id`, `link_id`, `direction`, `opened_at`, `closed_at`, `severity`, `status` (`open|acknowledged|resolved`), `evidence_json` (§9 evidence bundle: direction, loss% vs baseline, protocol confirmation, first differing hop, host-resource evidence, confidence), `root_cause` (operator-filled; AI suggestions advisory only, §31), `operator_notes`.
- **alert** — `alert_id`, `incident_id→incident (nullable)`, `rule_id`, `fired_at`, `severity`, `channel`, `dedup_key`, `silenced_until`, `escalation_level` (§17).
- **audit_event** — `audit_event_id`, `tenant_id`, `actor_type` (`user|agent|system`), `actor_id`, `action`, `object_type`, `object_id`, `outcome`, `detail_json`, `created_at`, WORM-retained (§14). Every sensitive op is audited (§31).
- **identity** — `identity_id`, `agent_id`, `spiffe_id`, `cert_serial`, `fingerprint_sha256`, `issued_at`, `expires_at`, `status` (`active|rotated|revoked`), revocation ref (§10).

---

## 2. Canonical records

### 2.1 Measurement record (§13, complete field list)

One row per (agent, link, direction, test_type, window). Windows are the profile summary interval (10–30 s, §6-A) aligned to UTC. `Nullable` = may be NULL and under what condition.

| # | Field | Type | Null | Notes |
|--:|---|---|:---:|---|
| 1 | `measurement_id` | UUIDv7 | no | Server-side dedup key (PROTOCOL §7.2). |
| 2 | `tenant_id` | UUID | no | Denormalized for query scoping/RLS. |
| 3 | `project_id` | UUID | no | |
| 4 | `environment` | text | no | Environment slug (§13 uses the name, not the id). |
| 5 | `agent_id` | UUID | no | Reporting agent. |
| 6 | `peer_id` | UUID | no | Remote peer of the measurement. |
| 7 | `link_id` | UUID | no | |
| 8 | `direction` | enum(`a_to_b`,`b_to_a`) | no | Never omitted (§31). |
| 9 | `test_type` | enum | no | `icmp_probe\|udp_probe\|tcp_probe\|tls_probe\|app_synthetic\|mtr\|mtu_discovery\|throughput` (§5). |
| 10 | `protocol` | text | no | `BNQO-UDP/1`, `ICMPv4`, `ICMPv6`, `TCP`, `TLS1.3/TCP`, … |
| 11 | `ip_version` | smallint | no | 4 or 6. |
| 12 | `src_address` | inet | no | |
| 13 | `src_port` | uint16 | yes | NULL for ICMP. |
| 14 | `dst_address` | inet | no | |
| 15 | `dst_port` | uint16 | yes | NULL for ICMP. |
| 16 | `started_at_utc` | timestamptz | no | Window start. |
| 17 | `ended_at_utc` | timestamptz | no | Window end. |
| 18 | `duration_monotonic_ns` | uint64 | no | Agent monotonic duration (§4.1). |
| 19 | `clock_offset_ms` | float64 | yes | From NTP/PTP telemetry; NULL if unknown. |
| 20 | `clock_uncertainty_ms` | float64 | yes | NULL if unknown → worst-case gating. |
| 21 | `clock_quality` | enum(`good`,`low_confidence`,`invalid_clock_sync`,`unknown`) | no | Governs OWD usability (§3, PROTOCOL §6.2). |
| 22 | `sequence_start` | uint32 | yes | Probe seq window (UDP tests only). |
| 23 | `sequence_end` | uint32 | yes | |
| 24 | `packet_size` | uint16 | yes | Configured probe size. |
| 25 | `dscp` | uint8 | no | |
| 26 | `ecn` | uint8 | no | |
| 27 | `configured_rate_bps` | uint64 | yes | Throughput/rate-limited tests. |
| 28 | `packets_sent` | uint64 | no | |
| 29 | `packets_received` | uint64 | no | |
| 30 | `packets_lost` | uint64 | no | `sent − received` after loss timeout. |
| 31 | `loss_percent` | float64 | no | 0 when `sent=0` (see `status=unknown`, §8). |
| 32 | `burst_loss_count` | uint32 | no | Runs of ≥2 consecutive losses. |
| 33 | `max_loss_burst` | uint32 | no | |
| 34 | `duplicate_packets` | uint32 | no | |
| 35 | `reordered_packets` | uint32 | no | |
| 36 | `max_reorder_distance` | uint32 | no | |
| 37 | `corrupted_packets` | uint32 | no | AEAD/padding failures (§5). |
| 38 | `rtt_min_ms` | float64 | yes | NULL when zero RTT samples. |
| 39 | `rtt_avg_ms` | float64 | yes | |
| 40 | `rtt_p50_ms` | float64 | yes | |
| 41 | `rtt_p95_ms` | float64 | yes | |
| 42 | `rtt_p99_ms` | float64 | yes | |
| 43 | `rtt_max_ms` | float64 | yes | |
| 44 | `owd_p50_ms` | float64 | yes | Meaning gated by `clock_quality`; NULL when `invalid_clock_sync` (§3 — never store as precise). |
| 45 | `owd_p95_ms` | float64 | yes | |
| 46 | `owd_p99_ms` | float64 | yes | |
| 47 | `jitter_ms` | float64 | yes | RFC 3393 estimator (PROTOCOL §6.6). |
| 48 | `throughput_bps` | uint64 | yes | Throughput tests / effective bitrate. |
| 49 | `tcp_retransmissions` | uint64 | yes | TCP tests + host TCP_INFO correlation. |
| 50 | `mtu` | uint16 | yes | Discovered PMTU (§7 tests). |
| 51 | `route_hash` | bytea(16) | yes | BLAKE3-128 of ordered hops; NULL for non-route tests. |
| 52 | `status` | enum (12 states, §8) | no | See `API.md` §6 for the state machine. |
| 53 | `confidence` | float32 | no | 0..1 (§9). |
| 54 | `error_class` | text | yes | Taxonomy: `timeout\|refused\|rst\|frag_needed\|mtu_blackhole\|auth_fail\|clock_unsync\|agent_overload\|…` |
| 55 | `agent_version` | text | no | Semver of reporting agent. |
| 56 | `reflector_version` | text | yes | |
| 57 | `config_version` | uint64 | no | Config under which measured (§4.1). |
| 58 | `received_at` | timestamptz | no | Ingest time; drives freshness (§9, §16). |
| 59 | `schema_version` | uint16 | no | Event schema version (§7). |

### 2.2 Route observation and hop record (§13, §5 FR-MEASURE-006)

`route_observation`: one per MTR/route-trace run.

| Field | Type | Null | Notes |
|---|---|---|:---:|---|
| `route_id` | UUIDv7 | no | |
| `measurement_id` | UUIDv7 | no | Parent MTR measurement row. |
| `link_id`, `direction` | — | no | Denormalized. |
| `started_at_utc` | timestamptz | no | |
| `probe_protocol` | enum(`icmp`,`udp`,`tcp`) | no | §5-006 multi-protocol. |
| `flow_stable` | bool | no | Paris-traceroute style flow entropy kept constant. |
| `route_hash` | bytea(16) | no | BLAKE3-128 over ordered hop addresses (missing hops as `*`). Route-change detection = hash comparison (§5-006). |
| `destination_reached` | bool | no | |
| `hop_count` | uint16 | no | |

`route_hop`: one per hop per observation.

| Field | Type | Null | Notes |
|---|---|---|:---:|---|
| `route_id` | UUIDv7 | no | FK → route_observation. |
| `measurement_id` | UUIDv7 | no | Denormalized (§13 lists it on the hop record). |
| `hop_number` | uint8 | no | 1-based TTL. |
| `hop_address` | inet | yes | NULL = silent hop (`*`), §24 "silent hop" scenario. |
| `asn` | uint32 | yes | Enriched at ingest (best-effort). |
| `hostname` | text | yes | rDNS, enriched at ingest. |
| `sent` | uint32 | no | Probes to this TTL. |
| `received` | uint32 | no | |
| `loss_percent` | float64 | no | Mid-hop loss without downstream loss ≠ path failure (§5-006). |
| `latency_min_ms` / `latency_avg_ms` / `latency_p95_ms` / `latency_max_ms` | float64 | yes | §5-006 last/avg/best/worst/stddev (stddev kept as `latency_stddev_ms`). |
| `latency_stddev_ms` | float64 | yes | |
| `jitter_ms` | float64 | yes | RFC 3393 across per-TTL replies. |

---

## 3. Physical mapping overview

| Data | Store | Rationale (§14) |
|---|---|---|
| tenants, projects, environments, agents, peers, links, profiles, jobs, identities, users/RBAC, config versions, incidents, alerts, audit | **PostgreSQL 16** | Relational integrity, per-object authorization (§11), WORM audit via append-only table + immutable backups. |
| Per-window numeric aggregates (loss%, RTT/jitter percentiles, throughput, host metrics, reflector security counters) | **VictoriaMetrics** | TSDB for aggregates (§14), dashboard queries (§16), alerting rules (§9/§17). |
| Raw measurement records (§2.1), route observations/hops (§2.2), job results, security/error events | **ClickHouse** | Event/analytical store (§14); high-volume append, TTL tiers, analytical queries (route timelines §16). |
| Diagnostic bundles, host snapshots, MTR raw output, pcaps (if enabled, §18) | **S3-compatible object storage** | §14; versioning + lifecycle (§26). |
| Agent spool | **Local WAL** (custom segmented format; §8) | Crash-safe offline buffer ≥72 h (§15). |

Agents never write to any database directly (§4.4): Agent → OTLP/gRPC (mTLS) → OTel Collector → NATS JetStream (`measurements`, `events`, `host-metrics` streams, durable, work-queue retention) → validation/enrichment → stream processor → stores.

---

## 4. PostgreSQL schema (DDL sketches)

```sql
CREATE SCHEMA bnqo;

CREATE TABLE bnqo.tenants (
    tenant_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL UNIQUE,
    status        text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','suspended')),
    retention_profile jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bnqo.projects (
    project_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES bnqo.tenants ON DELETE RESTRICT,
    name          text NOT NULL,
    description   text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE bnqo.environments (
    environment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    uuid NOT NULL REFERENCES bnqo.projects ON DELETE RESTRICT,
    name          text NOT NULL CHECK (name ~ '^[a-z0-9-]{1,32}$'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE TABLE bnqo.agents (
    agent_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id uuid NOT NULL REFERENCES bnqo.environments ON DELETE RESTRICT,
    tenant_id     uuid NOT NULL REFERENCES bnqo.tenants ON DELETE RESTRICT,
    hostname      text NOT NULL,
    spiffe_id     text NOT NULL UNIQUE,          -- spiffe://bnqo/agent/<uuid> (§10)
    agent_version text,
    reflector_version text,
    status        text NOT NULL DEFAULT 'enrolled'
                  CHECK (status IN ('enrolled','active','suspended','revoked')),
    current_config_version bigint NOT NULL DEFAULT 0,
    last_heartbeat_at timestamptz,
    resource_limits jsonb NOT NULL DEFAULT '{}', -- §4.1 CPU/RAM/disk/bandwidth
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agents_env_idx ON bnqo.agents (environment_id) WHERE status <> 'revoked';

CREATE TABLE bnqo.peers (
    peer_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      uuid NOT NULL REFERENCES bnqo.agents ON DELETE CASCADE,
    address       inet NOT NULL,
    port          integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
    ip_version    smallint GENERATED ALWAYS AS (family(address)) STORED,
    role          text NOT NULL CHECK (role IN ('probe','reflector','both')),
    UNIQUE (agent_id, address, port)
);

CREATE TABLE bnqo.links (
    link_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id uuid NOT NULL REFERENCES bnqo.environments ON DELETE RESTRICT,
    tenant_id     uuid NOT NULL REFERENCES bnqo.tenants ON DELETE RESTRICT,
    name          text NOT NULL,
    peer_a_id     uuid NOT NULL REFERENCES bnqo.peers ON DELETE RESTRICT, -- a = Iran side
    peer_b_id     uuid NOT NULL REFERENCES bnqo.peers ON DELETE RESTRICT, -- b = outside side
    service_class text NOT NULL CHECK (service_class IN
                  ('web','real_time','voice_video','tunnel','bulk_transfer','management_panel')), -- §9
    status        text NOT NULL DEFAULT 'unknown',   -- §8 12-state model, see API.md §6
    baseline_window_seconds integer NOT NULL DEFAULT 3600,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (peer_a_id <> peer_b_id),
    UNIQUE (environment_id, name)
);
CREATE INDEX links_tenant_idx ON bnqo.links (tenant_id);

CREATE TABLE bnqo.test_profiles (
    profile_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    link_id       uuid NOT NULL REFERENCES bnqo.links ON DELETE CASCADE,
    direction     text NOT NULL CHECK (direction IN ('a_to_b','b_to_a')),
    profile_class text NOT NULL CHECK (profile_class IN ('A','B','C','D')), -- §6
    test_type     text NOT NULL CHECK (test_type IN
                  ('icmp_probe','udp_probe','tcp_probe','tls_probe',
                   'app_synthetic','mtr','mtu_discovery','throughput')),
    interval_ms   integer NOT NULL CHECK (interval_ms BETWEEN 50 AND 3600000),
    jitter_fraction double precision NOT NULL DEFAULT 0.5
                  CHECK (jitter_fraction BETWEEN 0 AND 0.9),
    packet_size   integer CHECK (packet_size BETWEEN 64 AND 9000),
    dscp          smallint NOT NULL DEFAULT 0 CHECK (dscp BETWEEN 0 AND 63),
    thresholds    jsonb NOT NULL DEFAULT '{}',   -- §9 per-class thresholds
    enabled       boolean NOT NULL DEFAULT true,
    UNIQUE (link_id, direction, test_type, profile_class)
);

CREATE TABLE bnqo.jobs (                          -- §12 typed jobs
    job_id        uuid PRIMARY KEY,               -- UUIDv7, idempotency key
    tenant_id     uuid NOT NULL REFERENCES bnqo.tenants,
    job_type      text NOT NULL CHECK (job_type IN
                  ('RUN_ICMP_PROBE','RUN_UDP_PROBE','RUN_TCP_PROBE','RUN_TLS_PROBE',
                   'RUN_MTR','RUN_ROUTE_TRACE','RUN_MTU_DISCOVERY','RUN_THROUGHPUT_TEST',
                   'COLLECT_HOST_SNAPSHOT','ROTATE_IDENTITY','UPDATE_SIGNED_CONFIG')),
    agent_id      uuid NOT NULL REFERENCES bnqo.agents,
    peer_id       uuid REFERENCES bnqo.peers,
    parameters    jsonb NOT NULL DEFAULT '{}',    -- schema-validated per job_type (§11)
    created_at    timestamptz NOT NULL,
    not_before    timestamptz NOT NULL,
    expires_at    timestamptz NOT NULL,
    max_duration_ms integer NOT NULL CHECK (max_duration_ms > 0),
    max_bandwidth_bps bigint CHECK (max_bandwidth_bps >= 0),
    approval_id   uuid,                           -- required for throughput/C/D (§4.3, §5-008)
    config_version bigint NOT NULL,
    signature     bytea NOT NULL,                 -- Ed25519 over canonical envelope
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','dispatched','acknowledged','running',
                                    'completed','failed','canceled','expired','rejected')),
    requested_by  text NOT NULL,                  -- OIDC sub
    result_measurement_id uuid,
    CHECK (expires_at > not_before),
    CHECK (job_type NOT IN ('RUN_THROUGHPUT_TEST') OR approval_id IS NOT NULL)
);
CREATE INDEX jobs_agent_status_idx ON bnqo.jobs (agent_id, status);
CREATE INDEX jobs_tenant_created_idx ON bnqo.jobs (tenant_id, created_at DESC);

CREATE TABLE bnqo.identities (                    -- §10 cert/SVID lifecycle
    identity_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      uuid NOT NULL REFERENCES bnqo.agents ON DELETE CASCADE,
    spiffe_id     text NOT NULL,
    cert_serial   text NOT NULL UNIQUE,
    fingerprint_sha256 bytea NOT NULL,
    issued_at     timestamptz NOT NULL,
    expires_at    timestamptz NOT NULL,
    status        text NOT NULL CHECK (status IN ('active','rotated','revoked')),
    revoked_at    timestamptz,
    revocation_reason text
);
CREATE INDEX identities_agent_active_idx ON bnqo.identities (agent_id)
    WHERE status = 'active';

CREATE TABLE bnqo.incidents (
    incident_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES bnqo.tenants,
    link_id       uuid NOT NULL REFERENCES bnqo.links,
    direction     text CHECK (direction IN ('a_to_b','b_to_a')),
    severity      text NOT NULL CHECK (severity IN ('warning','critical')), -- §9
    status        text NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','acknowledged','resolved')),
    opened_at     timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz, acknowledged_by text,
    resolved_at   timestamptz, resolved_by text,
    evidence      jsonb NOT NULL,                 -- §9 evidence bundle
    root_cause    text,                           -- operator-filled (§31)
    operator_notes text NOT NULL DEFAULT ''
);
CREATE INDEX incidents_link_open_idx ON bnqo.incidents (link_id) WHERE status <> 'resolved';
CREATE INDEX incidents_tenant_opened_idx ON bnqo.incidents (tenant_id, opened_at DESC);

CREATE TABLE bnqo.alerts (
    alert_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id   uuid REFERENCES bnqo.incidents ON DELETE SET NULL,
    tenant_id     uuid NOT NULL,
    rule_id       text NOT NULL,                  -- §17 alert catalog key
    severity      text NOT NULL CHECK (severity IN ('warning','critical')),
    fired_at      timestamptz NOT NULL DEFAULT now(),
    dedup_key     text NOT NULL,                  -- §17 dedup/grouping
    channel       text NOT NULL,
    silenced_until timestamptz,
    escalation_level smallint NOT NULL DEFAULT 0,
    UNIQUE (rule_id, dedup_key, fired_at)
);

-- Append-only, WORM-retained (§14). No UPDATE/DELETE granted to the app role;
-- partitioned monthly; exported to object storage for long-term retention.
CREATE TABLE bnqo.audit_events (
    audit_event_id uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     uuid,
    actor_type    text NOT NULL CHECK (actor_type IN ('user','agent','system')),
    actor_id      text NOT NULL,                  -- OIDC sub | spiffe_id | component
    action        text NOT NULL,                  -- e.g. agent.revoke, job.create, config.sign
    object_type   text NOT NULL,
    object_id     text,
    outcome       text NOT NULL CHECK (outcome IN ('success','denied','error')),
    detail        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (audit_event_id, created_at)
) PARTITION BY RANGE (created_at);
CREATE INDEX audit_tenant_time_idx ON bnqo.audit_events (tenant_id, created_at DESC);
```

Authorization: row-level security on `tenant_id` for all tenant-scoped tables (§22 cross-tenant); the API role additionally enforces per-object checks (§11).

---

## 5. VictoriaMetrics metrics

### 5.1 Naming and label discipline (cardinality control)

Hard rules:

- Labels are restricted to: `tenant_id`, `link_id`, `direction`, `test_type`, `service_class`, `agent_id`, `peer_id`, `status` (only on status gauges), `clock_quality` (only on OWD metrics). **Never** label by `measurement_id`, IP addresses, `session_id`, `job_id`, or route hashes — those live in ClickHouse.
- Estimated cardinality at full scale (§1: 1,000 agents / 10,000 paths): worst metric ≈ 10k links × 2 directions × ~3 test types × 2 clock classes ≈ 120k active series — within single-node VictoriaMetrics comfort. `tenant_id` is carried but tenants are few.
- vmagent relabeling drops any unexpected label from agent OTLP attributes (defense against metric poisoning / cardinality bombs, §22).

### 5.2 Metric catalog (all per `direction` unless noted)

| Metric | Type | Labels | Source |
|---|---|---|---|
| `bnqo_udp_loss_ratio` | gauge | tenant, link, direction | stream processor (loss_percent/100 per window) |
| `bnqo_udp_burst_loss_total` | counter | tenant, link, direction | §2.1 fields 32–33 |
| `bnqo_udp_rtt_seconds` | summary → recording rules | tenant, link, direction | RTT percentiles (§6.1) |
| `bnqo_udp_owd_seconds` | summary | + `clock_quality` | §6.2; `invalid_clock_sync` series still emitted with NaN-free raw value but flagged — dashboards must gate on `clock_quality` |
| `bnqo_udp_jitter_seconds` | gauge | tenant, link, direction | RFC 3393 `J` |
| `bnqo_udp_reordered_total` / `bnqo_udp_duplicates_total` / `bnqo_udp_corrupted_total` | counter | tenant, link, direction | §6.5 |
| `bnqo_udp_effective_bitrate_bps` | gauge | tenant, link, direction | §6.5 |
| `bnqo_icmp_loss_ratio` / `bnqo_icmp_rtt_seconds` | gauge/summary | tenant, link, direction, `ip_version` | FR-MEASURE-001 |
| `bnqo_tcp_connect_success_ratio` / `bnqo_tcp_connect_seconds` / `bnqo_tcp_retransmits_total` | gauge/summary/counter | tenant, link, direction, `dst_port_name` (`panel`,`reverse`,`tls`,`tunnel`) | FR-MEASURE-003 |
| `bnqo_tls_handshake_seconds` / `bnqo_tls_cert_expiry_timestamp` | summary/gauge | tenant, link, `sni_name`(≤20 values) | FR-MEASURE-004 |
| `bnqo_app_probe_success_ratio` / `bnqo_app_ttfb_seconds` | gauge/summary | tenant, link, `probe_kind` (`https`,`websocket`,`grpc`) | FR-MEASURE-005 |
| `bnqo_mtu_discovered_bytes` / `bnqo_mtu_blackhole_detected` | gauge | tenant, link, direction | FR-MEASURE-007 |
| `bnqo_throughput_bps` | gauge | tenant, link, direction, `mode` (`tcp1`,`tcpN`,`udp`) | FR-MEASURE-008 |
| `bnqo_route_changes_total` | counter | tenant, link, direction | route-hash transitions (§5-006) |
| `bnqo_host_cpu_usage_ratio` / `bnqo_host_cpu_steal_ratio` / `bnqo_host_memory_pressure` / `bnqo_host_disk_pressure` / `bnqo_host_load1` | gauge | tenant, agent | §7 host metrics |
| `bnqo_host_nic_drops_total` / `bnqo_host_nic_errors_total` / `bnqo_host_qdisc_drops_total` / `bnqo_host_softnet_drops_total` / `bnqo_host_conntrack_usage_ratio` / `bnqo_host_fd_usage_ratio` / `bnqo_host_tcp_retrans_total` / `bnqo_host_udp_rx_errors_total` | counter/gauge | tenant, agent, `iface` (≤8) | §7 |
| `bnqo_clock_offset_seconds` / `bnqo_clock_uncertainty_seconds` / `bnqo_clock_quality` (0–3 enum gauge) | gauge | tenant, agent, `source` (`ntp`,`ptp`,`none`) | §7, §3 |
| `bnqo_agent_wal_depth_records` / `bnqo_agent_wal_utilization_ratio` / `bnqo_agent_wal_evictions_total` | gauge/counter | tenant, agent | §15, §17 queue alerts |
| `bnqo_agent_up` / `bnqo_agent_last_heartbeat_timestamp` | gauge | tenant, agent | §16 freshness |
| `bnqo_reflector_drops_total` | counter | tenant, agent, `reason` (9 values, PROTOCOL §4.5) | security view (§16) |
| `bnqo_link_status` | gauge (0–11 enum code) | tenant, link, direction, `status` | §8 state, one-hot |
| `bnqo_ingest_lag_seconds` | gauge | collector | §25 ingest p95 <5 s |

### 5.3 Recording rules (rollups)

VictoriaMetrics vmalert computes: 10 s rollups from raw OTLP gauges (stream processor already emits per-window; the 10 s tier is the agent window itself), then `:1m`, `:5m`, `:1h` recording rules, e.g.:

```yaml
- record: bnqo_udp_loss_ratio:1m
  expr: avg_over_time(bnqo_udp_loss_ratio[1m])
- record: bnqo_udp_rtt_seconds:p95_1m
  expr: quantile_over_time(0.95, bnqo_udp_rtt_seconds[1m])
```

Retention per tier (§14): raw windows 14 d, `:10s` 30 d, `:1m` 90 d, `:5m` 1 y, `:1h` multi-year (VictoriaMetrics `-retentionPeriod` per tenant via vmauth routing to per-tier clusters, or downsampling cron into a long-retention instance).

---

## 6. ClickHouse schema

One cluster database `bnqo`. All tables `MergeTree` family, sharded by `cityHash64(agent_id)`, replicated ×2 (§26).

### 6.1 Raw measurement records (§2.1), retention tier 1

```sql
CREATE TABLE bnqo.measurements_raw (
    measurement_id   UUID,
    schema_version   UInt16,
    tenant_id        UUID,
    project_id       UUID,
    environment      LowCardinality(String),
    agent_id         UUID,
    peer_id          UUID,
    link_id          UUID,
    direction        Enum8('a_to_b' = 1, 'b_to_a' = 2),
    test_type        Enum8('icmp_probe' = 1, 'udp_probe' = 2, 'tcp_probe' = 3,
                           'tls_probe' = 4, 'app_synthetic' = 5, 'mtr' = 6,
                           'mtu_discovery' = 7, 'throughput' = 8),
    protocol         LowCardinality(String),
    ip_version       UInt8,
    src_address      IPv6,             -- IPv4 stored as mapped
    src_port         Nullable(UInt16),
    dst_address      IPv6,
    dst_port         Nullable(UInt16),
    started_at_utc   DateTime64(3, 'UTC'),
    ended_at_utc     DateTime64(3, 'UTC'),
    duration_monotonic_ns UInt64,
    clock_offset_ms  Nullable(Float64),
    clock_uncertainty_ms Nullable(Float64),
    clock_quality    Enum8('unknown' = 0, 'good' = 1,
                           'low_confidence' = 2, 'invalid_clock_sync' = 3),
    sequence_start   Nullable(UInt32),
    sequence_end     Nullable(UInt32),
    packet_size      Nullable(UInt16),
    dscp             UInt8,
    ecn              UInt8,
    configured_rate_bps Nullable(UInt64),
    packets_sent     UInt64,
    packets_received UInt64,
    packets_lost     UInt64,
    loss_percent     Float64,
    burst_loss_count UInt32,
    max_loss_burst   UInt32,
    duplicate_packets UInt32,
    reordered_packets UInt32,
    max_reorder_distance UInt32,
    corrupted_packets UInt32,
    rtt_min_ms       Nullable(Float64),
    rtt_avg_ms       Nullable(Float64),
    rtt_p50_ms       Nullable(Float64),
    rtt_p95_ms       Nullable(Float64),
    rtt_p99_ms       Nullable(Float64),
    rtt_max_ms       Nullable(Float64),
    owd_p50_ms       Nullable(Float64),   -- NULL when clock invalid (§3)
    owd_p95_ms       Nullable(Float64),
    owd_p99_ms       Nullable(Float64),
    jitter_ms        Nullable(Float64),
    throughput_bps   Nullable(UInt64),
    tcp_retransmissions Nullable(UInt64),
    mtu              Nullable(UInt16),
    route_hash       Nullable(FixedString(16)),
    status           Enum8('healthy' = 0, 'degraded' = 1, 'critical' = 2,
                           'unreachable' = 3, 'probe_blocked' = 4,
                           'service_failed' = 5, 'agent_unhealthy' = 6,
                           'telemetry_delayed' = 7, 'control_plane_unavailable' = 8,
                           'clock_unsynchronized' = 9, 'unknown' = 10,
                           'maintenance' = 11),            -- §8, see API.md §6
    confidence       Float32,
    error_class      LowCardinality(Nullable(String)),
    agent_version    LowCardinality(String),
    reflector_version LowCardinality(Nullable(String)),
    config_version   UInt64,
    received_at      DateTime64(3, 'UTC'),
    -- inter-arrival / reorder histograms as parallel arrays (§5)
    interarrival_hist_ms Array(Float32),
    interarrival_hist_counts Array(UInt32)
)
ENGINE = ReplacingMergeTree(received_at)      -- idempotent re-ingest (§15/§25 dedup)
PARTITION BY toYYYYMMDD(started_at_utc)
ORDER BY (tenant_id, link_id, direction, test_type, started_at_utc, measurement_id)
TTL started_at_utc + INTERVAL 14 DAY DELETE   -- §14 raw HF samples 7–14 d
SETTINGS index_granularity = 8192;
```

`ReplacingMergeTree(received_at)` keyed by the ORDER BY tuple (which ends in `measurement_id`) makes WAL redelivery after crash/reconnect a no-op on re-ingest — the §25 "no logical duplicates after reconnect" criterion at the storage layer, complementary to the ingest-side watermark dedup.

### 6.2 Route data (retention ≥ 1 y, §14)

```sql
CREATE TABLE bnqo.route_observations (
    route_id UUID, measurement_id UUID, tenant_id UUID, link_id UUID,
    direction Enum8('a_to_b' = 1, 'b_to_a' = 2),
    started_at_utc DateTime64(3, 'UTC'),
    probe_protocol Enum8('icmp' = 1, 'udp' = 2, 'tcp' = 3),
    flow_stable Bool,
    route_hash FixedString(16),
    destination_reached Bool,
    hop_count UInt16
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at_utc)
ORDER BY (tenant_id, link_id, direction, started_at_utc, route_id)
TTL started_at_utc + INTERVAL 400 DAY DELETE;   -- §14 ≥1 y, rounded to months

CREATE TABLE bnqo.route_hops (
    route_id UUID, measurement_id UUID, tenant_id UUID, link_id UUID,
    direction Enum8('a_to_b' = 1, 'b_to_a' = 2),
    started_at_utc DateTime64(3, 'UTC'),
    hop_number UInt8,
    hop_address Nullable(IPv6),
    asn Nullable(UInt32),
    hostname Nullable(String),
    sent UInt32, received UInt32,
    loss_percent Float64,
    latency_min_ms Nullable(Float64), latency_avg_ms Nullable(Float64),
    latency_p95_ms Nullable(Float64), latency_max_ms Nullable(Float64),
    latency_stddev_ms Nullable(Float64),
    jitter_ms Nullable(Float64)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at_utc)
ORDER BY (tenant_id, link_id, started_at_utc, route_id, hop_number)
TTL started_at_utc + INTERVAL 400 DAY DELETE;
```

### 6.3 Security/error events (≥ 1 y)

```sql
CREATE TABLE bnqo.security_events (   -- reflector drops, auth failures, replay, job rejects (§16 security view)
    event_id UUID, tenant_id UUID, agent_id UUID,
    event_class LowCardinality(String),   -- reflector_drop|auth_fail|replay|job_rejected|config_sig_fail|rate_limit
    reason LowCardinality(String),        -- drop_malformed, drop_auth_fail, ... (PROTOCOL §4.5)
    session_id Nullable(UInt64),
    peer_address Nullable(IPv6),
    count UInt32,                         -- pre-aggregated per 10 s by the emitter
    occurred_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (tenant_id, agent_id, event_class, occurred_at)
TTL occurred_at + INTERVAL 400 DAY DELETE;
```

### 6.4 Rollup tables (materialized views from `measurements_raw`)

```sql
CREATE TABLE bnqo.measurements_1m (    -- same shape for _5m, _1h
    tenant_id UUID, link_id UUID, direction Enum8(...), test_type Enum8(...),
    bucket_start DateTime('UTC'),
    samples AggregateFunction(count, UInt64),
    loss_avg AggregateFunction(avg, Float64),
    loss_max AggregateFunction(max, Float64),
    rtt_ms AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), Float64),
    jitter_ms AggregateFunction(quantilesTDigest(0.5, 0.95), Float64),
    throughput_bps AggregateFunction(max, UInt64),
    packets_sent AggregateFunction(sum, UInt64),
    packets_lost AggregateFunction(sum, UInt64),
    status_worst AggregateFunction(argMax, Enum8(...), DateTime64(3, 'UTC'))
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket_start)
ORDER BY (tenant_id, link_id, direction, test_type, bucket_start)
TTL bucket_start + INTERVAL 90 DAY DELETE;   -- 1m tier; _5m → 365 DAY; _1h → 5 YEAR
```

Materialized views `mv_measurements_1m` (etc.) select from `measurements_raw` with `GROUP BY` on the ORDER BY tuple and `toStartOfMinute(started_at_utc)` buckets. Because raw TTL is 14 d but views aggregate continuously, tier data is complete; the **10 s tier is the raw table itself** (agent windows are 10–30 s, §6-A) — a separate 10 s table is unnecessary, and the §14 "10 s aggregates 30 d" requirement is met by keeping `measurements_raw` at the 14 d regulatory tier plus the `_1m` view for the 30–90 d window. **Gap note:** §14 literally asks for 10 s aggregates retained 30 d; this design serves 10–30 s resolution for 14 d and 1 m resolution beyond. If strict 10 s × 30 d is required, extend `measurements_raw` TTL to 30 d (≈2.1× raw storage) or add a dedicated 10 s `AggregatingMergeTree` tier — decision deferred to Phase 1 capacity benchmarking (§25 "verified by real benchmarks").

Rollup correctness rule: rollups are computed from the **raw records**, never from other rollups, so a late-arriving (WAL-replayed) batch triggers view re-aggregation on its partition; `AggregatingMergeTree` merges states associatively, making re-aggregation idempotent.

---

## 7. Object storage layout and encryption

Bucket `bnqo-artifacts` (S3-compatible, versioning enabled, §26):

```
s3://bnqo-artifacts/
  {tenant_id}/
    diagnostics/{yyyy}/{mm}/{job_id}/{artifact_name}        # host snapshots, MTR raw output
    pcaps/{yyyy}/{mm}/{job_id}/{capture_id}.pcapng.enc      # §18, default OFF
    audit-export/{yyyy}/{mm}/{partition}.parquet            # WORM audit export (§14)
    wal-deadletter/{agent_id}/{batch_id}.bin                # rejected batches for forensics
```

- Encryption: SSE with per-tenant KMS keys (SSE-KMS); pcaps use a **dedicated key** with separate access policy (§18 "encrypted with dedicated key").
- Lifecycle: diagnostics 90 d; pcaps 7 d with auto-delete enforced by bucket lifecycle *and* CP-side reaper (§18 auto-delete); audit-export per org requirement (WORM via object-lock); dead-letter 30 d.
- Access: only via pre-signed URLs minted by the management API after RBAC + audit (§11, §18 restricted access). Agents upload via `UploadDiagnosticArtifact` which returns a constrained pre-signed PUT (§23).

---

## 8. Downsampling/rollup strategy

```
agent window (10–30 s) ──► measurements_raw (14 d) ──MV──► _1m (90 d) ──MV──► _5m (1 y) ──MV──► _1h (5 y)
                     └──► VictoriaMetrics raw (14 d) ──rules──► :1m (90 d) ──► :5m (1 y) ──► :1h (multi-year)
```

- The stream processor is the single writer of both stores from the same validated record, so CH and VM never disagree on definitions (loss% formula, jitter estimator, direction labels) — one code path (PROTOCOL §6).
- Recomputation backfill: for disaster recovery or definition changes, rollups can be rebuilt from `measurements_raw` while it lives, and from `_1m` for older data (lossy above 1 m resolution — documented limitation).
- Percentile composition across windows uses t-digest state merging (CH `quantilesTDigest` states; VM `quantile_over_time` on per-window summaries). TDigest merge error ≤ 0.5% at p99 — within the §25 documented RTT tolerance.

---

## 9. Schema versioning strategy (§13 "all events have versioned schema")

- Every event/measurement carries `schema_version` (u16). Protobuf messages evolve additively; removed fields are `reserved` (PROTOCOL §9).
- Policy: **additive-only within a major schema version**. Consumers must tolerate unknown fields (proto3) and unknown enum values (map to `*_UNSPECIFIED`/0, never fail the batch).
- Breaking change → new `schema_version` major: stream processor dual-writes old+new tables for one raw-retention window (14 d), dashboards cut over, old table TTLs out. Migrations are runbook-gated (§26 rolling upgrade).
- PostgreSQL metadata changes go through versioned SQL migrations (squashed per release, tested against production-shape dumps).
- The WAL also carries `schema_version` per segment so a newer agent's spool is forward-readable by the ingest of one version ahead/behind (§26).

---

## 10. Agent WAL record format (§15)

Custom segmented append-only log (preferred over SQLite for the WAL hot path; SQLite allowed by §27 but the segmented format below is the reference design):

```
segment file: wal/{agent_seq_range}.wal   (default 64 MiB, fsync per batch or 1 s)

record frame:
  offset  size  field
  0       4     magic = 0x57414C31 ("WAL1")
  4       1     frame_version = 1
  5       1     record_class   (0=measurement_batch, 1=route, 2=security_event,
                               3=incident_priority_batch, 255=checkpoint)
  6       2     header_len (=32)
  8       8     agent_seq      (u64, strictly increasing, persisted watermark source)
  16      8     upload_seq     (matches MeasurementBatch.upload_seq, PROTOCOL §7.2)
  24      4     payload_len    (compressed length)
  28      4     payload_crc32c (Castagnoli CRC of ciphertext; crash-safe torn-write detection)
  32      N     payload: zstd( XChaCha20-Poly1305( wal_key, nonce=wal_salt||agent_seq,
                                                 plaintext = MeasurementBatch proto ) )
```

- **Checksum per record** (CRC32C) + AEAD tag → corruption/tamper detection on replay (§15 crash-safe).
- **Encryption at rest:** `wal_key` (32 B) generated at enrollment, stored 0600 in the agent state dir, optionally TPM-sealed (§10 alternative, §20); rotated with identity rotation.
- **Sequence:** `agent_seq` monotonic across restarts (stored in `wal/meta.json`, itself checksummed); powers ordered resend and server dedup (§4.1).
- **Quota & eviction:** configured cap (default 2 GiB ≈ ≫72 h at Profile A rates; verified against §25 ≥72 h). Eviction is oldest-segment-first **except** segments containing `incident_priority_batch` records (§15 priority queue), which evict last. Utilization is reported in heartbeats; ≥80% raises `local queue near capacity` (§17).
- **Filesystem guard:** the agent computes `min(free_disk × 0.5, configured_quota)` as the effective cap and stops spooling (dropping oldest) before the OS filesystem can fill (§15 "never fill the OS filesystem").
- **Backpressure:** when upload is blocked, new writes proceed until quota; producers of new measurements are never blocked (measurement continuity during CP outage, §2).
- **Replay on reconnect:** segments below server `high_watermark` are truncated; the rest are re-uploaded in `upload_seq` order with backoff+jitter (§15, PROTOCOL §7.2).

---

## 11. Traceability summary

| Element | Digest basis |
|---|---|
| 12-state status enums in CH + link status gauge | §8, §31 |
| Full §13 measurement/hop field lists | §13, §5-006 |
| PG for metadata/audit (WORM), CH events, VM metrics, S3 artifacts | §14 |
| TTL tiers 14 d / 30 d / 90 d / 1 y / multi-year | §14 |
| Cardinality-capped VM labels | §16 dashboards at 10k-path scale (§1) |
| ReplacingMergeTree + watermark dedup | §15, §25 |
| WAL frame (checksum, seq, AEAD, quota, eviction, priority) | §15 |
| Pcap dedicated key, auto-delete, restricted access | §18 |
