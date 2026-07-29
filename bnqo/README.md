# BNQO probe agent (Rust)

Implementation of the BNQO Phase-1 probe agent for the eve control plane.
Normative references:

- `../docs/bnqo/EVE_API_CONTRACT.md` — HTTP contract (enroll, signed config,
  jobs, report batch, Ed25519 scheme, HKDF key derivation).
- `../docs/bnqo/PROTOCOL.md` — BNQO-UDP v1 wire format, AEAD, replay window,
  reflector validation order, measurement math.

## Layout

```
crates/bnqo-proto    packet codec + AEAD + replay window + signatures (pure, no I/O)
crates/bnqo-measure  measurement math + ICMP/TCP-TLS probers + host metrics
crates/bnqo-agent    the bnqo-agent binary (tokio)
```

## Build (Linux host, static musl binary)

The dependency set is pure Rust (tokio, chacha20poly1305, ed25519-dalek,
hkdf, sha2, serde/serde_json, reqwest with rustls-tls, tokio-rustls,
socket2, clap, toml, base64, hex, crc32c, rand). No OpenSSL/native-tls, no
zstd C library, no other C dependencies. One exception: rustls' default
`ring` crypto provider (pulled in via reqwest/tokio-rustls) compiles a small
amount of assembly/C with the target C toolchain; it is not a library
dependency and links statically.

```sh
rustup target add x86_64-unknown-linux-musl
sudo apt-get install musl-tools        # provides the musl C toolchain for ring's asm
cd bnqo
cargo build --release --target x86_64-unknown-linux-musl
install -m 0755 target/x86_64-unknown-linux-musl/release/bnqo-agent \
    ../static/app-files/bnqo/bnqo-agent   # served per contract §6 (gitignored)
```

If `musl-tools` is unavailable, `cargo zigbuild --release --target
x86_64-unknown-linux-musl` (zig as the linker) works as a drop-in.

## Test

```sh
cd bnqo
cargo test --workspace        # platform-independent suite; Linux bits are cfg-gated
cargo clippy --workspace --all-targets
```

## Install / run

```sh
sudo install -m 0755 bnqo-agent /usr/local/bin/bnqo-agent
sudo useradd --system --home /var/lib/bnqo --shell /usr/sbin/nologin bnqo
sudo mkdir -p /etc/bnqo
sudo tee /etc/bnqo/agent.toml <<'EOF'
eve_url = "https://<eve-origin>"
name = "de-fra-1"
state_dir = "/var/lib/bnqo"
bind_port = 44818
EOF
sudo install -m 0644 bnqo-agent.service /etc/systemd/system/bnqo-agent.service
sudo systemctl daemon-reload
# Enrollment is first-run: set BNQO_ENROLL_TOKEN in the unit's Environment or
# via `systemctl edit bnqo-agent` before the first start.
sudo systemctl enable --now bnqo-agent
```

### systemd unit (`bnqo-agent.service`)

Hardening per `docs/bnqo/SECURITY_CONTROLS.md` (§19 runtime hardening;
dedicated user, only CAP_NET_RAW, no privilege escalation, read-only system,
private /tmp, state dir managed by systemd):

```ini
[Unit]
Description=BNQO probe agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bnqo
Group=bnqo
ExecStart=/usr/local/bin/bnqo-agent --config /etc/bnqo/agent.toml
Restart=always
RestartSec=5

# First-run enrollment (delete after successful enroll):
# Environment=BNQO_ENROLL_TOKEN=

# Raw ICMP sockets (SOCK_RAW fallback) without running as root.
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
NoNewPrivileges=true

ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
StateDirectory=bnqo
StateDirectoryMode=0750

[Install]
WantedBy=multi-user.target
```

## Configuration (`agent.toml`)

| key | default | meaning |
|---|---|---|
| `eve_url` | — | control-plane origin |
| `name` | — | agent name used at enrollment |
| `state_dir` | `/var/lib/bnqo` | identity, WAL, last-good config |
| `bind_port` | `44818` | UDP probe/reflector port |
| `role` | `outside` | advisory role at enrollment |
| `enroll_token` | — | one-time enroll token (env `BNQO_ENROLL_TOKEN` wins) |
| `control_poll_interval_sec` | `10` | config/jobs poll period (+ up to 2 s jitter) |
| `report_flush_interval_sec` | `10` | report batch seal period |
| `max_clock_skew_sec` | `300` | reflector timestamp sanity bound (§4.3 step 6) |
| `wal_quota_bytes` | `67108864` | 64 MB spool quota, oldest-segment eviction |
| `wal_fsync` | `true` | fsync every batch append |

## Documented deviations from the specs

1. **Session material (PROTOCOL.md §3.1 vs EVE_API_CONTRACT.md §2.2).** The
   Phase-1 contract config carries no `session_id`/`test_id`/`key_epoch`/
   `not_before`/`not_after` per link. The agent therefore uses
   `session_id = link_id`, `test_id = link_id (u32)`, `key_epoch = 0`, and
   does not enforce session lifetimes (reflector step 3 checks only the
   session-table tuple). The full §3.2 HKDF derivation
   (`SessionKeys::derive`) is implemented and tested in `bnqo-proto` for
   when the CP starts issuing session material.
2. **Nonce salt derivation.** Contract §2.2 specifies only the two 32-byte
   directional packet keys (HKDF-SHA256, salt `"bnqo-v1"`, info `"a_to_b"`/
   `"b_to_a"`). The AEAD nonce construction (PROTOCOL.md §2.5) also needs a
   12-byte per-direction nonce salt; it is derived from the same seed/PRK
   with info `"a_to_b_salt"`/`"b_to_a_salt"`, and the padding keystream key
   with info `"payload"`. The two packet keys themselves are exactly per the
   contract (cross-checked against a Python computation in tests).
3. **gRPC control stream (PROTOCOL.md §7).** Phase 1 uses the contract's
   HTTPS REST endpoints (config/jobs/report) instead of the gRPC stream; the
   §7.4 anti-rollback, §12 job checks, and §7.2-style at-least-once WAL
   upload with seq watermark are implemented over REST as the contract
   requires.
4. **OWD clock correction (§6.2).** Only the local host's clock offset is
   available (chrony/ntpq telemetry); the reflector's offset is not on the
   wire in Phase 1. Forward OWD is corrected with the local offset only and
   is always gated by the §6.2 quality rules (`unknown` ⇒ no value stored;
   `invalid`/`low` ⇒ value stored but flagged).
5. **Percentiles.** Nearest-rank (no interpolation).
6. **ICMP.** IPv4 only; Linux `SOCK_DGRAM` ping socket preferred,
   `SOCK_RAW` fallback (CAP_NET_RAW), `probe-blocked` reported elsewhere.
7. **`tcp_retrans`** is the RetransSegs delta since the previous sample
   (0 on the first sample after start).
