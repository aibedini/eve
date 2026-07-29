# BNQO — Implementation Plan (Phase 0–6)

Status: Phase-0 planning document. Normative source: `docs/bnqo/RFP_DIGEST.md` (cited as §N). Companion documents: `TEST_STRATEGY.md`, `SLO.md`.

Note on §29: the digest folds §28–30 into one section and does not enumerate §29's output list verbatim. The 35 mandatory outputs below are reconstructed from §28 (repo layout), §30 (phase definitions), and the normative component sections (§4–§23); each output is traceable to at least one digest section. If the original RFP §29 list surfaces, this table must be reconciled against it.

## 1. Scope and assumptions

- Initial deployment scope (§RFP intro): 1 Iran server, 1 outside server, 1 independent control plane. Architecture must not foreclose 1,000 agents / 10,000 paths (§1, §4.4).
- Agent and reflector are written in Rust (§27: "Rust preferred"); backend control-plane services in Rust or Go (decision recorded as ADR-0001 in Phase 0; this plan assumes Go for CP services, Rust for ingest hot path — revisit in Phase 0).
- Frontend: TypeScript/React (§27). Deployment: Ansible for VMs, Terraform for infra, Docker Compose dev-only, K8s for CP at scale (§27).
- No code is reused from the legacy Python pulse system (see §9 of this plan).
- Every phase ships build + tests + docs + security review (§31). Security and failure tests land alongside the code they cover, not in a later phase (§31).

## 2. Mandatory outputs (35) mapped to phases

| # | Output | Phase | Digest ref |
|---|--------|-------|-----------|
| 1 | Scope & requirements traceability matrix | 0 | §1, §30 |
| 2 | Threat model (STRIDE, per-threat table w/ owner + validation test) | 0 | §22 |
| 3 | Wire protocol spec: secure UDP packet format (protobuf + custom binary header) | 0 | §3, §5-002 |
| 4 | Control/telemetry API spec: protobuf + OpenAPI 3.1 | 0 | §23 |
| 5 | Data model spec: measurement/route records, versioned schemas | 0 | §13 |
| 6 | Identity model spec: SPIFFE/SVID or private-CA design, enrollment, rotation, revocation | 0 | §10 |
| 7 | Storage model spec: TSDB/ClickHouse/PostgreSQL/object store, retention tiers | 0 | §14 |
| 8 | ADR set (tech stack, WAL design, queue choice, clock strategy) | 0 | §27, §30 |
| 9 | SLO document (`SLO.md`) | 0 | §25, §26, §30 |
| 10 | Implementation plan + test strategy (this file, `TEST_STRATEGY.md`) | 0 | §30 |
| 11 | `bnqo-proto` crate: packet + control protos, codegen, versioned schemas | 1 | §5-002, §13, §23 |
| 12 | Secure UDP probe engine (STAMP-based, RFC 8762; PDV per RFC 3393) | 1 | §3, §5-002 |
| 13 | Secure reflector (authenticated peers only, anti-amplification, replay window) | 1 | §4.2 |
| 14 | ICMP probe module | 1 | §5-001 |
| 15 | TCP probe module (real service ports, TCP_INFO) | 1 | §5-003 |
| 16 | TLS probe module | 1 | §5-004 |
| 17 | MTR/route analysis module (Paris traceroute, route hash) | 1 | §5-006 |
| 18 | MTU/PMTU discovery module | 1 | §5-007 |
| 19 | Local WAL (crash-safe, checksummed, encrypted, ≥72h, idempotent resend) | 1 | §4.1, §15 |
| 20 | Clock-sync validation & reporting (NTP/PTP offset/uncertainty, OWD gating) | 1 | §3, §4.1 |
| 21 | Host/kernel metrics collector | 1 | §7 |
| 22 | Enrollment service (one-time token, fingerprint check, audit) | 2 | §10 SR-IDENTITY-002 |
| 23 | Identity issuance/rotation/revocation service | 2 | §10, §4.3 |
| 24 | Signed config service + config versioning + agent-side verification | 2 | §4.1, §12, §31 |
| 25 | Job scheduler (typed jobs only, approval workflow for heavy tests) | 2 | §4.3, §12 |
| 26 | Policy engine (agent authorization, per-link policy) | 2 | §10 SR-IDENTITY-003, §4.3 |
| 27 | Ingest gateway (OTLP/gRPC + mTLS, batch ACK, server-side dedup) | 2 | §4.4, §15, §23 |
| 28 | Agent inventory/registry on PostgreSQL | 2 | §4.3, §14 |
| 29 | Telemetry pipeline: collector → durable MQ → validation/enrichment → stream processor → stores | 3 | §4.4 |
| 30 | Dashboard web app (global/link/route/incident/security views) | 3 | §16 |
| 31 | Detection/incident engine (thresholds, baselines, correlation, confidence) | 3 | §8, §9 |
| 32 | Alerting: channels, dedup/grouping/silence/escalation | 3 | §17 |
| 33 | Security hardening bundle: SPIFFE/PKI deploy, runtime hardening profiles, RBAC, audit, secrets mgmt, supply-chain pipeline (SBOM + cosign), signed updates, pentest report | 4 | §10, §11, §19, §20, §21 |
| 34 | Validation suite results: full netem matrix, benchmarks, chaos, load, recovery, backup/restore, security acceptance report | 5 | §24, §25, §26 |
| 35 | Production rollout bundle: canary + staged rollout records, runbooks, monitor-the-monitor dashboards, post-deploy review | 6 | §26, §30 |

## 3. Phase-by-phase work breakdown

Team sizes assume mid/senior engineers; durations are calendar ranges including reviews. Durations overlap where dependency notes allow.

### Phase 0 — Design (§30)

- Goals: freeze scope, protocol, identity, data/storage models; threat model; ADRs; SLOs; this plan.
- Deliverables: outputs 1–10.
- Entry criteria: RFP digest ratified; sponsor sign-off on initial scope (1+1 servers, 1 CP).
- Exit criteria: threat model reviewed with per-threat validation tests mapped to `TEST_STRATEGY.md`; protocol/data/identity specs versioned v0.1; ADRs merged; no open "TBD" on §3, §10, §13, §15 decisions.
- Team/duration: 3–4 people (1 architect, 1–2 senior engineers, 0.5 security engineer), 4–6 weeks.
- Dependencies: none (project start). Security engineer availability gates the threat model.

### Phase 1 — Measurement core (§30)

- Goals: agent + reflector binaries that measure bidirectional loss/jitter/OWD/RTT/reorder/dup, ICMP/TCP/TLS/MTR/MTU, with WAL and clock validation, running autonomously without any control plane (static signed config for now).
- Deliverables: outputs 11–21.
- Entry criteria: Phase 0 exit; protocol v0.1 frozen; lab with ≥3 Linux VMs (netem-capable) available.
- Exit criteria (demo M1): two VMs under `tc netem`, bidirectional loss/jitter/OWD measured and stored via WAL; WAL survives `kill -9` mid-write with zero committed-record loss; OWD suppressed (`invalid-clock-sync`) when clock offset is forced out of bounds; pcap reconciliation within tolerance (§25); agent idle CPU <1%, RSS <150MB (§25).
- Team/duration: 3–5 engineers (2 measurement core, 1 WAL/storage, 1 test/automation, 0.5 security), 10–14 weeks (7 two-week sprints, see §4).
- Dependencies: Phase 0. Reflectors and agents co-developed; netem harness (see `TEST_STRATEGY.md`) is a sprint-1 deliverable.

### Phase 2 — Control plane (§30)

- Goals: enrollment, identity issuance/rotation, signed config distribution, typed-job scheduler, policy engine, ingest gateway, inventory — replacing Phase 1's static config.
- Deliverables: outputs 22–28.
- Entry criteria: Phase 1 exit; identity spec (output 6) frozen; PostgreSQL + dev K8s (kind/k3s) available.
- Exit criteria (demo M2): fresh agent enrolls with one-time token, receives mTLS identity, pulls signed config, executes scheduled typed jobs, uploads batches through ingest gateway with precise ACK; CP outage ≥2h does not stop measurements (agent WAL-spools, resends in order on reconnect, no logical duplicates); revoked cert rejected within the documented window (§25).
- Team/duration: 4–5 engineers (2 CP services, 1 identity/security, 1 agent integration, 1 test), 8–12 weeks (5 sprints, see §5).
- Dependencies: Phase 1 agent must already speak the control protocol against a mock (sprint 1.6) so Phase 2 integrates against a stable client.

### Phase 3 — Observability (§30)

- Goals: full telemetry pipeline per §4.4, storage tiers per §14, dashboard per §16, detection engine per §9, alerting per §17.
- Deliverables: outputs 29–32.
- Entry criteria: Phase 2 exit (ingest gateway accepting real agent traffic); ClickHouse/TSDB/MQ infra provisioned (Terraform).
- Exit criteria (demo M3): end-to-end — netem-injected 5% one-direction loss appears on dashboard <15s, incident opens with evidence bundle (direction, loss vs baseline, route-change correlation, host metrics, confidence), alert delivered via ≥2 channels with dedup; ingest p95 <5s under 10× initial-scope load (§25).
- Team/duration: 4–6 engineers (2 pipeline, 1 stream/detection, 1–2 frontend, 1 storage/SRE), 8–12 weeks.
- Dependencies: Phase 2. Frontend can start on mocked Query API after pipeline schema freeze (~sprint 3 of the phase).

### Phase 4 — Security hardening (§30)

- Goals: production identity (SPIFFE/SPIRE or private CA per ADR), runtime hardening profiles per §19, RBAC/audit per §11, secrets management per §20, supply-chain pipeline per §21 (SBOM, cosign, SLSA L3 target), signed agent updates, external pentest.
- Deliverables: output 33.
- Entry criteria: Phase 3 exit; threat model (output 2) current.
- Exit criteria: every §22 threat has an implemented required control + passing validation test; no open critical/high vulns (§25); pentest report with no unmitigated high findings; cosign verification enforced in deploy path.
- Team/duration: 3–4 engineers (2 security/platform, 1 agent, 1 CP) + external pentest firm, 6–10 weeks.
- Dependencies: Phases 1–3 (hardens existing system). Pentest scheduling is a lead-time risk — book in Phase 3.

### Phase 5 — Validation (§30)

- Goals: execute the complete §24 netem/failure/security matrix, §25 acceptance benchmarks, chaos/load/recovery, backup/restore drills per §26, security acceptance.
- Deliverables: output 34.
- Entry criteria: Phase 4 exit; netem harness (built in Phase 1, extended since) covers all §24 scenarios.
- Exit criteria: 100% of §24 scenarios pass or have documented waivers approved by sponsor; all §25 acceptance criteria verified by real benchmarks with published numbers; RPO/RTO demonstrated by restore drill (§26).
- Team/duration: 3–4 engineers + 0.5 SRE, 6–10 weeks.
- Dependencies: Phase 4. Long soak tests (72h offline) run in background from week 1 of the phase.

### Phase 6 — Production (§30)

- Goals: canary, staged rollout, SLOs live, runbooks, monitor-the-monitor, post-deploy review (§30).
- Deliverables: output 35.
- Entry criteria: Phase 5 exit; production infra provisioned; on-call rotation staffed.
- Exit criteria: SLO dashboards green for 30 consecutive days on production traffic; monitor-the-monitor alerting verified by synthetic incident injection; post-deploy review completed and actions tracked.
- Team/duration: 2–3 engineers + SRE/on-call, 4–8 weeks (30-day SLO observation window dominates).
- Dependencies: Phase 5.

### Summary timeline

| Phase | Duration | Team | Cumulative (with ~20% overlap) |
|-------|----------|------|-------------------------------|
| 0 Design | 4–6 wk | 3–4 | 0–6 wk |
| 1 Measurement core | 10–14 wk | 3–5 | 5–20 wk |
| 2 Control plane | 8–12 wk | 4–5 | 18–32 wk |
| 3 Observability | 8–12 wk | 4–6 | 30–44 wk |
| 4 Security hardening | 6–10 wk | 3–4 + pentest | 42–54 wk |
| 5 Validation | 6–10 wk | 3–4 | 50–64 wk |
| 6 Production | 4–8 wk | 2–3 | 58–72 wk |

Total: roughly **14–18 months** elapsed, peak team 5–6 engineers. Critical path: Phase 1 → Phase 2 → Phase 3.

## 4. Phase 1 sprint breakdown (measurement core)

Two-week sprints, dependency order. "Done" includes unit + integration + relevant netem tests (§31).

| Sprint | Tasks (in dependency order) | Exit demo |
|--------|------------------------------|-----------|
| 1.1 | Repo bootstrap per §6; CI stages fmt/clippy/test (§7); netem harness skeleton (2 namespaces + veth); `bnqo-proto` packet format + serde round-trip tests | CI green; harness applies loss/latency |
| 1.2 | WAL core: append format, per-record CRC, sequence numbers, quota + oldest-eviction, fsync policy; crash-fuzz loop (`kill -9` at random write offsets); WAL reader/replay | WAL survives kill loop, zero committed loss |
| 1.3 | Reflector: packet parse, HMAC/AEAD verify, session keys, replay window, anti-amplification (response ≤ request), rate limits; reflector fuzz target #1 | Reflector silent to forged/replayed packets |
| 1.4 | Agent UDP probe engine: send/receive, forward/reverse loss, burst loss, reorder/dup detect, jitter (RFC 3393), RTT; agent↔reflector over netem matrix subset (loss/latency/jitter) | Loss/jitter match pcap within tolerance |
| 1.5 | Clock module: NTP/PTP status/offset/uncertainty reporting, OWD computation, confidence gating (`invalid-clock-sync`/`low-confidence`); clock-drift netem test | OWD suppressed under forced drift |
| 1.6 | ICMP probe; TCP probe (connect metrics + TCP_INFO); control-protocol client stub (mock CP) for Phase 2 readiness; WAL→uploader with batch ACK + idempotent resend | All three probe types under netem |
| 1.7 | TLS probe; MTR module (Paris traceroute, route hash); MTU discovery (probe sizes per §5-007); host-metrics collector (§7); resource-limit enforcement; performance benchmark run | M1 demo: full Phase-1 exit criteria |

Key dependency chain: proto (1.1) → WAL (1.2, independent of network) and reflector (1.3) → probe engine (1.4) → clock/OWD (1.5) → remaining probes (1.6–1.7). WAL and reflector are parallelizable from sprint 1.2.

## 5. Phase 2 sprint breakdown (control plane)

| Sprint | Tasks (in dependency order) | Exit demo |
|--------|------------------------------|-----------|
| 2.1 | PostgreSQL schema (agents, links, configs, jobs, audit); gRPC service skeletons per §23; API auth middleware (mTLS identity parsing) | Services boot, schema migrations run |
| 2.2 | Enrollment service: one-time token issue/consume, fingerprint + node-attribute check, audit events; agent `EnrollAgent` flow end-to-end | Fresh agent enrolls, token single-use enforced |
| 2.3 | Identity service: cert issuance/rotation/revocation (interim private CA; SPIFFE swap-in per ADR in Phase 4); CRL/short-lifetime propagation; `RotateIdentity` job | Rotation without measurement gap; revoked cert rejected |
| 2.4 | Signed config: config compiler, signing key management, version monotonicity, agent-side verify + keep-last-valid; downgrade-rejection test | Tampered/old config rejected, last-valid kept |
| 2.5 | Job scheduler: typed jobs per §12, not_before/expires_at/max_duration/max_bandwidth enforcement, approval workflow for heavy tests, no-duplicate-execution guarantee; policy engine (per-link authorization, no arbitrary targets) | Out-of-policy job rejected + audited |
| 2.6 (half) | Ingest gateway: OTLP/gRPC mTLS, batch validation, precise ACK, server-side dedup (idempotency keys); load smoke at 10× initial scope | M2 demo: full Phase-2 exit criteria |

Dependency chain: schema (2.1) → enrollment (2.2) → identity (2.3) → signed config (2.4) → scheduler/policy (2.5) → ingest (2.6). Ingest depends only on 2.1 + 2.3 and can start at 2.3 in parallel with 2.4.

## 6. Repository bootstrap (§28)

Rust workspace; `cmd/*` are binary crates, `internal/*` are library crates (package names `bnqo-*`). Layout exactly per §28:

```bash
# root
mkdir bnqo && cd bnqo && git init
cat > Cargo.toml <<'EOF'
[workspace]
resolver = "2"
members = [
  "cmd/agent", "cmd/reflector", "cmd/control-plane",
  "cmd/ingest-gateway", "cmd/stream-processor", "cmd/cli",
  "internal/measurement", "internal/identity", "internal/policy",
  "internal/scheduler", "internal/telemetry", "internal/storage",
  "internal/security", "internal/audit",
]
[workspace.package]
edition = "2021"
license = "Proprietary"
EOF

# binaries (crate names per §28 roles)
cargo new --bin cmd/agent           --name bnqo-agent
cargo new --bin cmd/reflector       --name bnqo-reflector
cargo new --bin cmd/control-plane   --name bnqo-control
cargo new --bin cmd/ingest-gateway  --name bnqo-ingest
cargo new --bin cmd/stream-processor --name bnqo-stream
cargo new --bin cmd/cli             --name bnqo-cli

# libraries
cargo new --lib internal/measurement --name bnqo-measure
cargo new --lib internal/identity    --name bnqo-identity
cargo new --lib internal/policy      --name bnqo-policy
cargo new --lib internal/scheduler   --name bnqo-scheduler
cargo new --lib internal/telemetry   --name bnqo-telemetry
cargo new --lib internal/storage     --name bnqo-storage
cargo new --lib internal/security    --name bnqo-security
cargo new --lib internal/audit       --name bnqo-audit

mkdir -p proto api web deploy/{ansible,terraform,kubernetes,systemd} \
         configs tests/{unit,integration,e2e,netem,security,performance} \
         threat-model docs/{architecture,adr,runbooks,api,operations} \
         .github/workflows fuzz
```

`bnqo-proto` (output 11) lives at `proto/` with a `build.rs`-based codegen crate (`proto/` itself is a lib crate, added to `members`; name `bnqo-proto`). If any CP service is Go (ADR-0001), it lives under `cmd/<svc>/` as a Go module with its own `go.mod`; the workspace `Makefile`/`justfile` drives both toolchains.

Crate boundaries and dependency rules:

| Crate | Owns | May depend on |
|-------|------|---------------|
| `bnqo-proto` | packet format, control protos, versioned schemas (§13) | nothing internal |
| `bnqo-measure` | all probe engines, clock module, PDV math (§3, §5) | `bnqo-proto` |
| `bnqo-security` | crypto, signatures, replay windows, HMAC/AEAD (§4.2, §12) | `bnqo-proto` |
| `bnqo-storage` | WAL + client-side storage adapters (§14, §15) | `bnqo-proto` |
| `bnqo-agent` | agent runtime, uploader, host metrics, systemd glue (§4.1) | measure, security, storage, telemetry, proto |
| `bnqo-reflector` | reflector runtime (§4.2) | security, proto |
| `bnqo-telemetry` | OTLP/gRPC clients + server handlers (§4.4, §23) | proto, audit |
| `bnqo-identity` | enrollment, issuance, rotation, revocation (§10) | security, audit, proto |
| `bnqo-policy` | authorization, job policy (§10-003, §12) | proto, audit |
| `bnqo-scheduler` | job scheduling, approval workflow (§4.3, §12) | policy, identity, proto |
| `bnqo-audit` | audit event model + sinks (§4.3, §22) | proto |
| `bnqo-control` | CP composition root | identity, policy, scheduler, telemetry, storage |
| `bnqo-ingest` / `bnqo-stream` | ingest gateway / stream processor (§4.4) | telemetry, proto, audit |
| `bnqo-cli` | operator CLI | proto, API clients only |

Forbidden directions: `bnqo-proto` must never depend on runtime crates; agent-side crates must not depend on CP-only crates (`scheduler`, `identity` server halves) — enforced by `cargo-deny` bans + a CI workspace-dependency lint.

## 7. CI pipeline (§21)

`.github/workflows/` — stages in order; per-PR vs nightly split defined in `TEST_STRATEGY.md` §9.

1. **fmt** — `cargo fmt --all -- --check` (+ `gofmt` if Go services exist).
2. **clippy/lint** — `cargo clippy --all-targets -- -D warnings`; `cargo doc` warnings denied.
3. **build** — locked (`--locked`), release + debug, `--target x86_64-unknown-linux-gnu` (and musl static for agent if ADR selects it).
4. **test** — `cargo nextest run` unit+integration; race/concurrency: `loom` model tests for lock-free WAL paths (Rust), `go test -race` for any Go services.
5. **miri/sanitizers** — `cargo +nightly miri test` for crates containing `unsafe` (target: zero unsafe outside audited FFI); Go services: ASAN/TSAN builds.
6. **fuzz** — `cargo-fuzz` (libfuzzer) short runs per-PR, long corpus runs nightly; targets listed in `TEST_STRATEGY.md` §6.
7. **SAST** — `cargo-geiger` (unsafe census), Semgrep; `gosec` for Go.
8. **dependency/secret/license/container/IaC scanning** — `cargo-deny` (advisories + license policy + bans), `osv-scanner`, `gitleaks`, `trivy` (containers), `checkov`/`tfsec` (Terraform/K8s).
9. **SBOM** — CycloneDX via `cargo-cyclonedx`, SPDX via `syft`; attached to every release artifact (§21, §31).
10. **build provenance + reproducibility** — SLSA L3 target via GitHub OIDC (`slsa-github-generator`); reproducible-build check job rebuilds and compares digests where feasible (§21).
11. **signing** — `cosign sign-blob` (keyless, Fulcio) for binaries and container images; signatures + attestations pushed with releases.
12. **verify-before-deploy** — deploy jobs (Ansible/K8s admission) run `cosign verify` and refuse unsigned artifacts; staged rollout + canary per §21 in Phase 6.

## 8. Milestone demos

| Milestone | End of | Demonstrable |
|-----------|--------|--------------|
| M0 | Phase 0 | Ratified specs + threat model; ADRs; this plan reviewed |
| M1 | Phase 1 | Two VMs under `tc netem`: bidirectional loss/jitter/OWD measured + WAL-stored; WAL survives `kill -9`; OWD gated on clock confidence; resource budgets met |
| M2 | Phase 2 | Zero-touch enrollment → mTLS identity → signed config → scheduled typed jobs → ingest ACK; 2h CP outage with lossless catch-up; revocation enforcement |
| M3 | Phase 3 | Injected one-direction 5% loss → dashboard <15s → incident with evidence bundle → multi-channel alert; ingest p95 <5s at 10× load |
| M4 | Phase 4 | All §22 threats have passing validation tests; signed/SBOM'd release; clean pentest; cosign-gated deploy |
| M5 | Phase 5 | Full §24 matrix green; §25 benchmark report published; RPO/RTO restore drill passed |
| M6 | Phase 6 | 30 days green SLOs in production; monitor-the-monitor catches synthetic incident; post-deploy review done |

## 9. Relationship to the legacy pulse system

- **Code reuse: none.** Pulse is a Python proxy-config health-checker (polling agent, bearer tokens); BNQO is a new Rust/Go system (§27). Bearer-token auth, arbitrary polling targets, and the pulse data model are incompatible with §10 (no static API keys), §12 (typed jobs), and §13 (versioned measurement schema). Nothing is imported, linked, or forked from `pulse*.py`.
- **Domain reuse (knowledge only):** pulse's operational history (which links flap, typical RTT baselines Iran↔outside, known ICMP-blocking behavior) informs baseline thresholds and test profiles (§6, §9). This is data, not code.
- **Coexistence (Phases 1–6):** pulse and BNQO run side by side on the same hosts. BNQO agents run under their own dedicated user/systemd unit with their own resource budgets (§19); probe ports and bandwidth are configured to not perturb pulse measurements. The `eve` panel remains pulse's control plane; BNQO has its own independent CP (§2).
- **Migration trigger:** once BNQO passes Phase 5 and covers pulse's server-to-server use cases (loss/latency/reachability between Iran and outside servers), pulse's server-to-server checks are put in compare-only shadow mode for one full SLO window (30 days), then disabled.
- **Deprecation path:** (1) Phase 6 +30 days: pulse server-to-server checks disabled, configs archived in the eve DB; (2) following minor release of eve: pulse server-to-server UI marked deprecated; (3) after two minor releases with zero re-enable requests: pulse agent code for those checks removed from eve per eve's own versioning policy (eve `AGENTS.md`). Pulse's other use cases (proxy-config probing) are out of BNQO scope and unaffected.

## 10. Risk register (plan-level)

| ID | Risk | Likelihood | Impact | Mitigation | Owner |
|----|------|-----------|--------|-----------|-------|
| R1 | Clock-sync accuracy insufficient for OWD over WAN (NTP jitter ms-scale, asymmetric paths) | High | OWD unusable without gating (§3) | Clock-quality reporting from day 1 (sprint 1.5); OWD stored only with confidence; evaluate PTP/Chrony + TS PCIe options in Phase 0 ADR; acceptance uses tolerance bands, not absolute ms claims | Measurement lead |
| R2 | UDP reflector becomes an attack surface (amplification, replay, DoS) — §4.2 | Medium | High | Response ≤ request hard invariant + property test; replay window fuzzed; rate/concurrency limits; binds to configured IP only; pentest in Phase 4; security scenarios in netem matrix | Security eng |
| R3 | WAL corruption or logical duplicates after crash/reconnect (§15, §25) | Medium | High | Per-record CRC + monotonic sequences; `kill -9` soak from sprint 1.2; server-side dedup with idempotency keys; fuzz WAL parser; crash-recovery integration tests in CI | Storage eng |
| R4 | Ingest/storage scale underestimated at 1,000 agents / 10,000 paths (§1, §4.4) | Medium | High | 10× initial-scope load test in Phase 2/3; capacity model in Phase 0 storage spec; MQ backpressure + priority queue for incidents; Phase 5 load test at projected scale with published numbers | SRE |
| R5 | Iran↔outside connectivity instability breaks lab realism or CP reachability (sanctions, filtering, asymmetric routing) | Medium | Medium | Netem matrix includes blocking/partition scenarios (§24); CP on independent third region (§2); agent autonomy + 72h WAL are hard requirements, tested early | Architect |
| R6 | Scope creep: BNQO absorbs eve-panel features (billing, user mgmt) | Medium | Medium | BNQO CP scope limited to §4.3; integration with eve only via read-only data export, out of phases 0–6 scope | PM/architect |
| R7 | Rust/Go split creates duplicate protocol/tooling burden | Low–Med | Medium | ADR-0001 decides in Phase 0; `bnqo-proto` is the single schema source; codegen for both languages from same protos | Architect |
| R8 | Pentest late findings force redesign (identity, job envelope) | Low | High | Threat model drives design in Phase 0; security acceptance criteria tested continuously (§25); book pentest during Phase 3 | Security eng |
| R9 | Key-person dependency on measurement-core engineer | Medium | Medium | Pairing on WAL/reflector; ADRs + design docs mandatory; code review by second engineer on every §4.1/§4.2 change | Eng manager |
| R10 | Legacy pulse deprecation blocked by operational habits | Medium | Low | Shadow-mode comparison data (§9) gives operators evidence; deprecation owned by eve maintainers with dated checkpoints | PM |
