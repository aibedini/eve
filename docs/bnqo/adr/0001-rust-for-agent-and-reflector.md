# ADR-0001: Rust for probe agent and secure reflector

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §4.1 (memory-safe language, Rust preferred), §4.2, §19 (runtime hardening), §25 (idle CPU <1%, memory <150MB), §27 (agent/reflector: Rust preferred, Tokio, rustls, protobuf, gRPC)

## Context

The probe agent and secure reflector run on customer/production servers with raw-socket
capability (`CAP_NET_RAW`), parse unauthenticated network input before cryptographic
verification, and must meet tight resource budgets (idle CPU <1%, RSS <150MB —
RFP §25). RFP §4.1 mandates a memory-safe language: "Rust preferred; Go acceptable
with threat model + fuzz tests." The reflector is the most exposed component in the
system: it binds a UDP socket on a measured server and must stay silent and correct
under hostile input (RFP §4.2, §22 threats: UDP reflection, replay, forged packets).

A memory-corruption bug in pre-auth packet parsing would be a direct RCE primitive on
every measured host. Go eliminates spatial memory bugs but retains data races on
shared state, a GC whose pauses distort microsecond-precision timestamping, and a
larger baseline RSS that pressures the 150MB budget.

## Decision

Implement `bnqo-agent` and `bnqo-reflector` in **Rust** (edition 2021, MSRV pinned,
`#![forbid(unsafe_code)]` in first-party crates; unavoidable unsafe confined to
audited dependencies).

Concrete stack:

- Async runtime: **tokio** (multi-thread, worker count capped at 2 for the agent).
- UDP: `tokio::net::UdpSocket` + `socket2` for `IP_TTL`/`IP_TOS`/timestamping
  sockopts; `nix` for `SO_TIMESTAMPING` where hardware/kernel timestamps are
  available (RTT tolerance per RFP §25).
- TLS: **rustls** 0.23 (ring/aws-lc-rs provider) for telemetry mTLS and TLS probes;
  no OpenSSL dependency.
- gRPC/protobuf: **tonic** + **prost** for control stream and OTLP export.
- Signatures/MAC: **ed25519-dalek** (job/config signatures), **chacha20poly1305**
  (reflector AEAD), **hmac**+**sha2** (STAMP-compat mode).
- Serialization on the WAL path: `prost` (same protobuf schemas as the wire).
- eBPF (optional, later): **aya** for TCP retransmission visibility beyond
  `TCP_INFO`.
- Fuzzing: `cargo-fuzz` (libFuzzer) targets for the pre-auth UDP packet parser,
  config decoder, and job decoder — run in CI per RFP §21.

## Consequences

Positive:

- No data races / no GC pauses: deterministic timestamping and jitter measurement;
  comfortably meets the <1% CPU / <150MB budgets (idle agent measured in the 10–20MB
  RSS class for comparable tokio services).
- Strong type system models the typed-job constraint (RFP §12) at compile time:
  the executor is an enum match with no stringly-typed command path.
- Single static binary simplifies Ansible deployment and seccomp surface (RFP §19).
- One language for agent, reflector, stream processor, and backend reduces the
  dependency-audit and SBOM surface (RFP §21).

Negative / costs:

- Smaller hiring pool than Go; steeper learning curve for contributors.
- Compile times longer; CI needs sccache.
- Some niceties (mature eBPF userspace, some OTel SDK features) are younger in Rust
  than in Go — mitigated by keeping eBPF optional and using the OTel collector
  (language-neutral) at the edge.

## Alternatives considered

- **Go**: acceptable per RFP §4.1 but requires a dedicated threat model and fuzz
  suite, and GC/runtime overhead works against the resource and timing budgets.
  Rejected as primary; retained as contingency if Rust staffing fails.
- **C/C++**: forbidden in practice — not memory-safe, violates RFP §4.1.
