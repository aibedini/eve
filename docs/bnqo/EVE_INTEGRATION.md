# BNQO ↔ eve Integration (Phase 1 implementation)

This document describes how the BNQO Phase-1 measurement core is implemented
**inside eve** (as opposed to the standalone enterprise architecture in the
other `docs/bnqo/` documents, which remain the long-term target). It is the
operational reference for the integrated system.

## Integration profile — deviations from the enterprise design

| Enterprise design (docs/bnqo/*) | eve-integrated Phase 1 | Rationale |
|---|---|---|
| Independent control plane on a third server | eve itself is the control plane | Single-operator deployment; eve already manages all servers |
| gRPC/OTLP telemetry, mTLS + SPIFFE/SPIRE identity | HTTPS REST/JSON; one-time enroll token → per-agent Ed25519 keypair + bearer token; CP-signed (Ed25519) configs and typed jobs | eve is Flask; no gRPC/SPIRE stack. Replay protection via `X-BNQO-Timestamp` + signature, ±300 s skew |
| TSDB (VictoriaMetrics) + ClickHouse | SQLAlchemy tables in eve's DB (SQLite/PostgreSQL) + hourly rollups | Operational simplicity; raw retention 14 days, hourly rollups long-term |
| Kafka/NATS queue, stream processor | In-process 15 s scheduler tick (`panel/jobs/bnqo.py`) | Phase-1 scale (tens of links) |
| Rust agent (same) | Rust agent (same) — `bnqo/` workspace | Per ADR-0001 |
| Agent local WAL in SQLite | Agent local WAL = custom binary spool (pure Rust, no C deps) | Static musl build without a C toolchain |

The normative wire contract for the integrated system is
`docs/bnqo/EVE_API_CONTRACT.md`. The probe packet format stays `PROTOCOL.md`
(BNQO-UDP v1).

## Components

- **Agent** (`bnqo/crates/*`, binary `bnqo-agent`): Rust, tokio. Roles
  `iran` / `outside` / `relay` (a relay is just an agent; links are defined
  between any pair). Runs the BNQO-UDP prober + secure reflector, ICMP
  cycles, TCP/TLS service-target probes, host metrics, MTR jobs (system
  `mtr`), local WAL with strictly-increasing `agent_seq`, config signature
  verification with anti-rollback, signed typed-job executor (no shell).
- **Control plane** (in eve):
  - Models: `panel/models/ops.py` — `BnqoAgent`, `BnqoEnrollToken`,
    `BnqoLink`, `BnqoMeasurement`, `BnqoServiceProbe`, `BnqoRoute(Hop)`,
    `BnqoIncident`, `BnqoRollup`, `BnqoJob`.
  - Agent + admin API and pages: `panel/routes/bnqo.py`
    (`/api/bnqo/agent/*`, `/api/bnqo/*`, `/pulse/links*`).
  - Signing: `panel/services/bnqo_crypto.py` — CP Ed25519 key at
    `instance/bnqo_cp_key` (auto-generated, mode 0600).
  - Status engine + retention + alerts: `panel/jobs/bnqo.py`, ticked every
    15 s by the `bnqo_scheduler` singleton thread in the background process.
- **UI**: `templates/bnqo_links.html` (overview, agents, enroll tokens,
  incidents) and `templates/bnqo_link_detail.html` (per-direction
  loss/RTT/jitter charts, service targets, routes, incidents). Chart.js is
  vendored at `static/chart.umd.min.js` (no CDN). Navigation: "Links" in the
  sidebar next to Pulse; banner on the Pulse page.
- **CLI**: `eve` → `[n] BNQO — Network Link Monitor` (setup.sh
  `bnqo_menu()`): SSH install, manual one-time install command, agent
  status, SSH uninstall, binary info.
- **Installer**: `static/app-files/bnqo/install.sh` — idempotent; creates
  the `bnqo` system user, downloads the binary, writes
  `/etc/bnqo/agent.toml` + a hardened `bnqo-agent.service`
  (CAP_NET_RAW, NoNewPrivileges, ProtectSystem=strict, …), starts it.
  `UNINSTALL=1` removes everything.

## Operating guide

### 1. Build the agent binary

The binary is **not** committed to git (`.gitignore`). Build on any Linux
host (or WSL) with Rust ≥ 1.75:

```sh
rustup target add x86_64-unknown-linux-musl
cd bnqo && cargo build --release --target x86_64-unknown-linux-musl
cp target/x86_64-unknown-linux-musl/release/bnqo-agent \
   ../static/app-files/bnqo/bnqo-agent
```

The dependency set is pure Rust (rustls, no OpenSSL), so a plain musl
target works without a cross C toolchain. See `bnqo/README.md` for details
and the systemd unit reference.

### 2. Install an agent on a server (Iran / outside / relay)

Web UI: `/pulse/links` → "New enroll token" → pick role + TTL → copy the
one-time install command. Or CLI: `eve` → `[n]` → `[1]` (SSH install;
paste the token from the web UI) or `[2]` (manual command for servers
without SSH):

```sh
curl -fsSL https://<eve>/static/app-files/bnqo/install.sh -o /tmp/bnqo-install.sh
sudo BNQO_EVE_URL=https://<eve> BNQO_ENROLL_TOKEN=<token> \
     BNQO_ROLE=outside bash /tmp/bnqo-install.sh
```

The agent enrolls on first run (one-time token → agent token + keypair),
then polls eve for its signed config and starts probing. Check with
`systemctl status bnqo-agent` / `journalctl -u bnqo-agent -f`.

### 3. Define a link

`/pulse/links` → "Create link" → pick agent A and agent B. Each link
measures **both directions independently** (A→B and B→A). Optional profile
overrides: probe interval, packet size, window, ICMP toggle, and
`service_targets` (`host`/`port`/`tls`) — used for outbound→client-server
checks (agents only probe CP-provided targets, never arbitrary ones).

### 4. Read results

- Overview cards show per-direction status from the 12-state model
  (`healthy … unknown`; **no data is never shown as healthy**).
- Detail page: loss / RTT-p95 / jitter time series per direction, service
  target states, MTR hop tables with route-hash change highlighting,
  incident timeline with evidence, ack/resolve.
- Alerts: incidents (link down, sustained loss ≥ 5 %, degradation, service
  failure, agent offline, telemetry stale, route change) fan out over the
  same Telegram bot as pulse alerts. Critical/unreachable incidents
  auto-enqueue a diagnostic RUN_MTR (15 min cooldown).

### 5. Data retention

Raw measurement windows are kept 14 days, then folded into hourly rollups
(`bnqo_rollups_hourly`) by the same 15 s tick. Rollups are kept
indefinitely. Increase `RAW_RETENTION_DAYS` in `panel/jobs/bnqo.py` only if
the DB is PostgreSQL.

## Security notes (Phase 1)

- No static API keys: enrollment is one-time; every agent request is
  Ed25519-signed with a ±300 s timestamp window (replay protection).
- Configs and jobs are CP-signed; agents verify signature, monotonic
  `config_version` (anti-rollback), job expiry and type whitelist. There is
  no remote-shell job type and never will be.
- Probe traffic is authenticated (XChaCha20-Poly1305, HKDF-derived
  per-direction keys from a per-link seed); the reflector silently drops
  unauthenticated packets, caps duplicate reflections, and never amplifies
  (response ≤ request).
- The agent runs as an unprivileged `bnqo` user with `CAP_NET_RAW` only.
- CP private key: `instance/bnqo_cp_key` — back it up; losing it forces
  re-enrollment of all agents.

## Tests

- Rust: `cargo test --workspace` in `bnqo/` — 69 tests (codec, AEAD,
  replay window, HKDF/canonical-JSON vectors vs the Python rule, RFC 3393
  math, WAL crash-safety, config anti-rollback, contract-shaped batches).
- Python: `tests/test_bnqo_web.py` — 22 tests (enroll token lifecycle,
  signature auth, signed config + shared session seed, idempotent report,
  admin CRUD, series, status engine incl. unknown≠healthy, incidents).
