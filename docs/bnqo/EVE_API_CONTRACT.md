# BNQO ↔ eve Integration API Contract (Phase 1, normative)

This document is the **binding wire contract** between the Rust `bnqo-agent`,
the eve Flask control plane, the web UI, and the installer. All four
implementation tracks MUST conform to it exactly. Probe-packet wire format is
defined in `PROTOCOL.md` (BNQO-UDP v1) and is not repeated here.

Conventions:
- All times are UTC ISO-8601 with `Z` suffix.
- Agent-facing API is under `/api/bnqo/agent/*`; admin API under `/api/bnqo/*`;
  pages under `/pulse/links*`.
- Errors: JSON `{"error": {"code": "<snake_case>", "message": "<human>"}}` with
  a fitting HTTP status.

## 1. Identities and signatures

- eve CP holds an **Ed25519** keypair generated on first use, stored at
  `instance/bnqo_cp_key` (PEM, mode 0600). Public key: base64 raw 32 bytes.
- Each agent generates its own Ed25519 keypair at install time.
- **Canonical JSON** for signatures: UTF-8, keys sorted recursively, no
  whitespace, `ensure_ascii=False`, floats via `repr` shortest round-trip.
  (Python: `json.dumps(obj, sort_keys=True, separators=(",",":"))`.)
- CP signs configs and jobs with its Ed25519 private key; agents verify with
  the CP public key obtained at enrollment.
- Agents authenticate API calls with:
  - `Authorization: Bearer <agent_token>`
  - `X-BNQO-Timestamp: <unix seconds>`
  - `X-BNQO-Signature: <base64 Ed25519 signature over "<timestamp>\n" + raw body bytes>`
    (for GET with empty body, sign `"<timestamp>\n"`)
- Server rejects requests with timestamp skew > 300 s or invalid signature.

## 2. Agent API

### 2.1 `POST /api/bnqo/agent/enroll` (unauthenticated)

Request:
```json
{
  "enroll_token": "<one-time token>",
  "name": "de-fra-1",
  "role": "outside",              // iran | outside | relay (advisory; CP may override from token)
  "pubkey": "<base64 raw 32-byte Ed25519 public key>",
  "address": "203.0.113.10",       // optional; defaults to request remote addr
  "port": 44818,                   // UDP probe/reflector port the agent will bind
  "version": "0.1.0"
}
```
Response 200:
```json
{
  "agent_id": 3,
  "agent_token": "<64 hex>",
  "cp_pubkey": "<base64>",
  "config_version": 1
}
```
Errors: 404 `enroll_token_invalid`, 410 `enroll_token_expired`,
409 `enroll_token_used` / `agent_name_taken`.
The enroll token is invalidated atomically on success (single use).

### 2.2 `GET /api/bnqo/agent/config` (agent auth)

Response 200 — body is canonical-signed; `signature` covers the whole object
minus the `signature` field:
```json
{
  "config_version": 7,
  "agent": {"name": "de-fra-1", "role": "outside"},
  "links": [
    {
      "link_id": 1,
      "name": "IR-DE",
      "peer": {"name": "ir-tb-1", "address": "198.51.100.5", "port": 44818},
      "direction": "b_to_a",            // this agent is B on this link
      "session_seed": "<64 hex>",        // per-link, per-agent seed; HKDF-SHA256 derives directional keys
      "profile": {
        "interval_ms": 200,              // probe packet interval
        "packet_size": 256,
        "window_sec": 30,                // measurement window; one record per window per direction
        "icmp_enabled": true,
        "icmp_count": 5,
        "icmp_interval_sec": 30,
        "service_targets": [
          {"name": "panel", "host": "198.51.100.5", "port": 443, "tls": true, "interval_sec": 30}
        ]
      }
    }
  ],
  "signature": "<base64 Ed25519 by CP key>"
}
```
Key derivation (both sides): `key_ab = HKDF-SHA256(ikm=session_seed, salt="bnqo-v1", info="a_to_b", L=32)`,
`key_ba = ... info="b_to_a"`. Direction labels are from the link's A→B
perspective; each agent knows from `direction` whether it is A or B.

### 2.3 `GET /api/bnqo/agent/jobs` (agent auth)

```json
{
  "jobs": [
    {
      "job_id": "job_01J...",
      "type": "RUN_MTR",               // RUN_MTR | RUN_ICMP_PROBE | RUN_TCP_PROBE | COLLECT_HOST_SNAPSHOT
      "params": {"link_id": 1, "target": "198.51.100.5", "cycles": 10},
      "expires_at": "2026-07-29T01:00:00Z",
      "config_version": 7,
      "signature": "<base64 Ed25519 by CP key over object minus signature>"
    }
  ]
}
```
Agents MUST reject expired/unsigned/unknown-type jobs. Results return via the
report batch (`mtr_results`, `job_acks`); there is no separate ack endpoint.

### 2.4 `POST /api/bnqo/agent/report` (agent auth)

```json
{
  "agent_seq": 1234,                    // strictly increasing per agent; idempotent dedup key
  "sent_at": "2026-07-29T00:59:30Z",
  "measurements": [
    {
      "link_id": 1, "direction": "a_to_b",
      "window_start": "2026-07-29T00:59:00Z", "window_end": "2026-07-29T00:59:30Z",
      "sent": 150, "received": 148, "loss_pct": 1.33,
      "rtt_min_ms": 71.2, "rtt_avg_ms": 83.5, "rtt_p95_ms": 120.4, "rtt_max_ms": 210.0,
      "owd_ms": 41.0, "clock_quality": "good",      // good | low | invalid | unknown
      "jitter_ms": 6.2,
      "reordered": 0, "duplicated": 1, "corrupted": 0, "burst_max": 2
    }
  ],
  "icmp": [
    {"link_id": 1, "direction": "a_to_b", "sent": 5, "received": 5,
     "loss_pct": 0.0, "rtt_avg_ms": 82.1, "rtt_p95_ms": 95.3}
  ],
  "service_probes": [
    {"link_id": 1, "target_name": "panel", "ok": true,
     "tcp_ms": 80.2, "tls_ms": 41.5, "http_status": 200, "error_class": null}
  ],
  "host": {
    "cpu_pct": 3.1, "load1": 0.2, "mem_pct": 41.0, "disk_pct": 55.0,
    "rx_drops": 0, "tx_drops": 0, "tcp_retrans": 12,
    "clock_source": "chrony", "clock_offset_ms": 0.8
  },
  "mtr_results": [
    {"job_id": "job_01J...", "link_id": 1, "direction": "a_to_b",
     "route_hash": "<16 hex>", "destination_reached": true,
     "hops": [{"hop": 1, "address": "10.0.0.1", "loss_pct": 0.0, "rtt_avg_ms": 0.4}]}
  ],
  "job_acks": [{"job_id": "job_01J...", "status": "done", "error_class": null}]
}
```
Response 200: `{"accepted": true, "agent_seq": 1234, "duplicate": false}`.
Replay of an already-seen `agent_seq` returns `{"accepted": true, "duplicate": true}`
and stores nothing.

## 3. Admin API (session auth via `login_required`)

- `GET /api/bnqo/agents` → `{"agents":[{id,name,role,address,port,enabled,version,last_seen_at,last_ip,config_version}]}`
- `POST /api/bnqo/enroll-tokens` body `{"role":"outside","ttl_minutes":30}` →
  `{"token":"...","expires_at":"...","install_command":"curl -fsSL <origin>/static/app-files/bnqo/install.sh -o /tmp/bnqo-install.sh && sudo BNQO_EVE_URL=<origin> BNQO_ENROLL_TOKEN=<token> bash /tmp/bnqo-install.sh"}` (shown once)
- `POST /api/bnqo/agents/<id>/revoke` → disables the agent.
- `GET /api/bnqo/links` → `{"links":[{id,name,agent_a:{id,name,role},agent_b:{...},enabled,status,status_detail,last_data_at}]}`
- `POST /api/bnqo/links` body `{name, agent_a_id, agent_b_id, profile:{...}}` (profile optional; server defaults per §2.2)
- `PATCH /api/bnqo/links/<id>` (name, profile, enabled) · `DELETE /api/bnqo/links/<id>`
- `POST /api/bnqo/links/<id>/diagnose` → enqueues signed RUN_MTR jobs for both agents → `{"job_ids":[...]}`
- `GET /api/bnqo/links/<id>/series?metric=loss|rtt|jitter|owd&direction=a_to_b&hours=6`
  → `{"points":[{"t":"...","value":1.33}, ...]}` (falls back to hourly rollups beyond 14 days)
- `GET /api/bnqo/links/<id>/routes` → latest MTR per direction `{"routes":[{"direction":"a_to_b","route_hash":"...","created_at":"...","hops":[...]}]}`
- `GET /api/bnqo/incidents?status=open` → `{"incidents":[{id,link_id,link_name,direction,kind,status,evidence,opened_at,resolved_at}]}`
- `POST /api/bnqo/incidents/<id>/ack` · `POST /api/bnqo/incidents/<id>/resolve`

## 4. Pages

- `GET /pulse/links` → renders `bnqo_links.html`
- `GET /pulse/links/<id>` → renders `bnqo_link_detail.html`
Both use `login_required`; templates fetch data from the admin API above.

## 5. Status model (server-side, per link, stored on the link row)

`status` is one of: `healthy`, `degraded`, `critical`, `unreachable`,
`probe-blocked`, `service-failed`, `agent-unhealthy`, `telemetry-delayed`,
`control-plane-unavailable`, `clock-unsynchronized`, `unknown`, `maintenance`.
`status_detail` is a JSON object: per-direction sub-status + evidence.
Rules: no data in the last 3 windows ⇒ `unknown` (never `healthy`);
agent silent > 3 min ⇒ `agent-unhealthy`; data older than 2× window ⇒
`telemetry-delayed`; loss 100% ⇒ `unreachable`; loss ≥5% or repeated
micro-outages ⇒ `critical`; loss ≥1% over 3 windows or rtt_p95 > baseline+50%
⇒ `degraded`; UDP dead but ICMP alive ⇒ `probe-blocked`; UDP/ICMP fine but
service targets failing ⇒ `service-failed`.

## 6. Static artifacts

- Agent binary: `static/app-files/bnqo/bnqo-agent` (gitignored; produced by the
  build step — see `docs/bnqo/EVE_INTEGRATION.md`).
- Installer: `static/app-files/bnqo/install.sh` (tracked; takes
  `BNQO_EVE_URL` + `BNQO_ENROLL_TOKEN` env, downloads the binary, enrolls via
  §2.1, writes `/etc/systemd/system/bnqo-agent.service`, starts it).
